from telethon import TelegramClient
from flask import Flask, render_template, Response, jsonify, request, send_file, redirect, url_for, session
from authlib.integrations.flask_client import OAuth
from werkzeug.security import generate_password_hash, check_password_hash
import re
import asyncio
import os
import threading
import sqlite3


def load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        pass


load_dotenv()

api_id = os.getenv("TELEGRAM_API_ID")
api_hash = os.getenv("TELEGRAM_API_HASH")
channel = os.getenv("TELEGRAM_CHANNEL", "qchipobuyfinds")

if not api_id or not api_hash:
    raise RuntimeError("Missing TELEGRAM_API_ID or TELEGRAM_API_HASH environment variables")

api_id = int(api_id)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev_secret_change_me")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("FLASK_SESSION_SECURE", "0") == "1"

# --------- CONFIG ---------
POSTS_PAGE_SIZE = 30  # number of posts loaded per batch on the site
IMAGE_CACHE_MAX = 200
IMAGE_CACHE = {}
IMAGE_CACHE_ORDER = []
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache_images")
os.makedirs(CACHE_DIR, exist_ok=True)
PREFETCH_CONCURRENCY = 4

CACHE_LOCK = threading.Lock()
IN_FLIGHT = set()

DB_PATH = os.path.join(os.path.dirname(__file__), "app.db")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")

oauth = OAuth(app)
GOOGLE_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
if GOOGLE_ENABLED:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

CATEGORIES = {
    "haut": ["hoodie", "pull", "sweat", "tshirt", "tee"],
    "bas": ["cargo", "jean", "pant", "short"],
    "chaussure": ["sneaker", "shoes", "running"],
    "montre": ["watch", "rolex"],
    "accessoire": ["belt", "bag", "hat", "casquette"],
    "veste": ["jacket", "coat", "puffer"],
    "luxe": ["lv", "gucci", "prada"],
    "sport": ["nike", "adidas", "puma"],
    "tech": ["airpods", "iphone"],
    "bijoux": ["chain", "ring"],
}

BRANDS = [
    "nike",
    "adidas",
    "puma",
    "reebok",
    "under armour",
    "the north face",
    "patagonia",
    "columbia",
    "levi's",
    "levis",
    "wrangler",
    "diesel",
    "calvin klein",
    "tommy hilfiger",
    "ralph lauren",
    "lacoste",
    "hugo boss",
    "emporio armani",
    "gucci",
    "prada",
    "louis vuitton",
    "chanel",
    "dior",
    "versace",
    "balenciaga",
    "off-white",
    "off white",
    "supreme",
    "bape",
    "stone island",
    "moncler",
    "canada goose",
    "zara",
    "h&m",
    "uniqlo",
    "mango",
    "bershka",
    "pull&bear",
    "pull & bear",
    "massimo dutti",
    "stradivarius",
    "cos",
    "& other stories",
    "other stories",
    "shein",
    "boohoo",
    "prettylittlething",
    "asos",
    "forever 21",
    "gap",
    "old navy",
    "american eagle",
    "hollister",
    "abercrombie & fitch",
    "abercrombie and fitch",
    "burberry",
    "saint laurent",
    "valentino",
    "fendi",
    "givenchy",
    "balmain",
    "kenzo",
    "paco rabanne",
    "dsquared2",
    "philipp plein",
    "vetements",
    "acne studios",
    "sandro",
    "maje",
    "claudie pierlot",
    "sezane",
    "sézane",
    "ray-ban",
    "ray ban",
    "oakley",
    "tom ford",
    "persol",
    "oliver peoples",
    "maui jim",
    "costa del mar",
    "warby parker",
    "gentle monster",
    "ace & tate",
    "ace and tate",
    "specsavers",
    "alain afflelou",
    "atol",
    "rolex",
    "omega",
    "patek philippe",
    "audemars piguet",
    "cartier",
    "tag heuer",
    "breitling",
    "iwc",
    "jaeger-lecoultre",
    "jaeger lecoultre",
    "longines",
    "tissot",
    "seiko",
    "casio",
    "swatch",
    "hamilton",
    "tudor",
    "panerai",
    "bell & ross",
    "bell and ross",
    "hublot",
    "richard mille",
    "vacheron constantin",
    "blancpain",
    "stussy",
    "cp company",
    "trapstar",
    "new balance"
]

# --------- DETECTION ---------

def detect_category(text):
    text = text.lower()
    for cat, words in CATEGORIES.items():
        if any(w in text for w in words):
            return cat
    return "autre"


def detect_brand(text):
    text = text.lower()
    for brand in BRANDS:
        if brand in text:
            return brand
    return "autre"


def clean_links(text):
    links = re.findall(r"https?://\S+", text)
    hipobuy_links = [l for l in links if "hipobuy" in l]

    for l in links:
        if l not in hipobuy_links:
            text = text.replace(l, "")

    for l in hipobuy_links:
        text = text.replace(l, f'<a href="{l}" target="_blank">{l}</a>')

    return text


# --------- TELEGRAM ---------

_loop = asyncio.new_event_loop()
_loop_thread = threading.Thread(target=_loop.run_forever, daemon=True)
_loop_thread.start()

_client = TelegramClient("session", api_id, api_hash)


def run_async(coro):
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result()


async def _ensure_connected():
    if not _client.is_connected():
        await _client.connect()


def _cache_path(msg_id):
    return os.path.join(CACHE_DIR, f"{msg_id}.jpg")


def _cache_get(msg_id):
    with CACHE_LOCK:
        return IMAGE_CACHE.get(msg_id)


def _cache_put(msg_id, data):
    with CACHE_LOCK:
        IMAGE_CACHE[msg_id] = data
        IMAGE_CACHE_ORDER.append(msg_id)
        if len(IMAGE_CACHE_ORDER) > IMAGE_CACHE_MAX:
            old_id = IMAGE_CACHE_ORDER.pop(0)
            IMAGE_CACHE.pop(old_id, None)


def _mark_in_flight(msg_id):
    with CACHE_LOCK:
        if msg_id in IN_FLIGHT:
            return False
        IN_FLIGHT.add(msg_id)
        return True


def _unmark_in_flight(msg_id):
    with CACHE_LOCK:
        IN_FLIGHT.discard(msg_id)


async def fetch_posts_page(offset_id, limit):
    posts = []
    last_id = None

    await _ensure_connected()
    async for msg in _client.iter_messages(channel, limit=limit, offset_id=offset_id):
        if not msg.text and not msg.photo:
            continue

        text = msg.text or ""

        posts.append({
            "text": clean_links(text),
            "image_id": msg.id if msg.photo else None,
            "category": detect_category(text),
            "brand": detect_brand(text)
        })
        last_id = msg.id

    return posts, last_id


async def _fetch_image_bytes(msg_id):
    await _ensure_connected()
    msg = await _client.get_messages(channel, ids=msg_id)
    if not msg or not msg.photo:
        return None
    return await _client.download_media(msg.photo, file=bytes)


async def _prefetch_images(msg_ids):
    await _ensure_connected()
    sem = asyncio.Semaphore(PREFETCH_CONCURRENCY)

    async def _one(msg_id):
        if not _mark_in_flight(msg_id):
            return
        try:
            if _cache_get(msg_id) is not None:
                return
            cache_path = _cache_path(msg_id)
            if os.path.exists(cache_path):
                return
            async with sem:
                data = await _fetch_image_bytes(msg_id)
            if data is None:
                return
            _cache_put(msg_id, data)
            try:
                with open(cache_path, "wb") as f:
                    f.write(data)
            except Exception:
                pass
        finally:
            _unmark_in_flight(msg_id)

    await asyncio.gather(*[_one(mid) for mid in msg_ids])


def get_posts_page(offset_id, limit):
    return run_async(fetch_posts_page(offset_id, limit))


# --------- AUTH / USERS ---------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE,
                password_hash TEXT,
                google_sub TEXT UNIQUE,
                name TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def get_user_by_id(user_id):
    with get_db() as conn:
        return conn.execute(
            "SELECT id, email, name, google_sub FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()


def get_user_by_email(email):
    with get_db() as conn:
        return conn.execute(
            "SELECT id, email, name, google_sub, password_hash FROM users WHERE email = ?",
            (email,),
        ).fetchone()


def get_user_by_google_sub(sub):
    with get_db() as conn:
        return conn.execute(
            "SELECT id, email, name, google_sub FROM users WHERE google_sub = ?",
            (sub,),
        ).fetchone()


def create_user(email, password=None, name=None, google_sub=None):
    password_hash = generate_password_hash(password) if password else None
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, name, google_sub) VALUES (?,?,?,?)",
            (email, password_hash, name, google_sub),
        )
        conn.commit()
        return cur.lastrowid


def attach_google_sub(user_id, sub):
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET google_sub = ? WHERE id = ?",
            (sub, user_id),
        )
        conn.commit()


def set_user_session(user_id):
    user = get_user_by_id(user_id)
    if not user:
        return
    session["user_id"] = user["id"]
    session["user_email"] = user["email"]
    session["user_name"] = user["name"] or ""


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    row = get_user_by_id(uid)
    if not row:
        return None
    return {"id": row["id"], "email": row["email"], "name": row["name"]}


init_db()


@app.route("/image/<int:msg_id>")
def image(msg_id):
    cache_path = _cache_path(msg_id)
    if os.path.exists(cache_path):
        resp = send_file(cache_path, mimetype="image/jpeg", conditional=True)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    data = _cache_get(msg_id)
    if data is not None:
        try:
            with open(cache_path, "wb") as f:
                f.write(data)
        except Exception:
            pass
        return Response(
            data,
            mimetype="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"}
        )

    try:
        data = run_async(_fetch_image_bytes(msg_id))
    except Exception:
        return "", 503

    if data is None:
        return "", 404

    _cache_put(msg_id, data)

    try:
        with open(cache_path, "wb") as f:
            f.write(data)
    except Exception:
        pass

    return Response(
        data,
        mimetype="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"}
    )


@app.route("/")
def home():
    return render_template("index.html", user=current_user(), google_enabled=GOOGLE_ENABLED)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        name = request.form.get("name", "").strip()

        if not email or not password:
            error = "Email et mot de passe requis."
        elif len(password) < 6:
            error = "Mot de passe trop court (6 caractères min)."
        elif get_user_by_email(email):
            error = "Un compte existe déjà avec cet email."
        else:
            user_id = create_user(email, password=password, name=name)
            set_user_session(user_id)
            return redirect(url_for("home"))

    return render_template("signup.html", error=error, user=current_user(), google_enabled=GOOGLE_ENABLED)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = get_user_by_email(email)
        if not user or not user["password_hash"] or not check_password_hash(user["password_hash"], password):
            error = "Email ou mot de passe incorrect."
        else:
            set_user_session(user["id"])
            return redirect(url_for("home"))

    return render_template("login.html", error=error, user=current_user(), google_enabled=GOOGLE_ENABLED)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/auth/google/login")
def google_login():
    if not GOOGLE_ENABLED:
        return "Google OAuth non configuré.", 400
    redirect_uri = GOOGLE_REDIRECT_URI or url_for("google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/google/callback")
def google_callback():
    if not GOOGLE_ENABLED:
        return "Google OAuth non configuré.", 400
    try:
        token = oauth.google.authorize_access_token()
    except Exception:
        return "Erreur OAuth Google.", 400

    userinfo = token.get("userinfo")
    if not userinfo:
        try:
            userinfo = oauth.google.parse_id_token(token)
        except Exception:
            userinfo = None
    if not userinfo or not userinfo.get("email"):
        return "Impossible de récupérer votre profil Google.", 400

    email = userinfo.get("email", "").lower()
    name = userinfo.get("name", "")
    sub = userinfo.get("sub")

    user = get_user_by_google_sub(sub) if sub else None
    if not user:
        existing = get_user_by_email(email)
        if existing:
            if sub:
                attach_google_sub(existing["id"], sub)
            user_id = existing["id"]
        else:
            user_id = create_user(email, password=None, name=name, google_sub=sub)
    else:
        user_id = user["id"]

    set_user_session(user_id)
    return redirect(url_for("home"))


@app.route("/posts")
def posts():
    raw_offset = request.args.get("offset", "0")
    try:
        offset_id = max(0, int(raw_offset))
    except ValueError:
        offset_id = 0

    posts, next_offset = get_posts_page(offset_id, POSTS_PAGE_SIZE)
    image_ids = [p["image_id"] for p in posts if p.get("image_id")]
    if image_ids:
        try:
            asyncio.run_coroutine_threadsafe(_prefetch_images(image_ids), _loop)
        except Exception:
            pass
    return jsonify({
        "posts": posts,
        "next_offset": next_offset
    })
    

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
