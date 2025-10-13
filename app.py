# app.py
import os
import re
import json
import time as _time
import logging
import traceback
from datetime import datetime
from urllib.parse import urlparse, urljoin

import requests
from dotenv import load_dotenv
from flask import (
    Flask, render_template, request, redirect, url_for, jsonify, flash, send_from_directory
)
from werkzeug.utils import secure_filename
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    current_user, login_required
)
from sqlalchemy.exc import IntegrityError

# --------------------------
# Load environment variables
# --------------------------
load_dotenv()

# --------------------------
# Logging
# --------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
log = logging.getLogger("conserv")

# --------------------------
# Flask app & configuration
# --------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "supersecretkey")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///database.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# --------------------------
# Uploads configuration
# --------------------------
app.config["UPLOAD_FOLDER"] = os.getenv("UPLOAD_FOLDER", "uploads")
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_CONTENT_LENGTH", 25 * 1024 * 1024))  # 25 MB

ALLOWED_EXTS = {"png", "jpg", "jpeg", "webp", "gif", "pdf"}

def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTS

# Ensure upload folder exists at startup
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message_category = "warning"


# --- Delivery pricing config (env → floats) ---
from math import radians, sin, cos, asin, sqrt

BASE_LAT = float(os.getenv("DELIVERY_BASE_LAT", "0"))
BASE_LNG = float(os.getenv("DELIVERY_BASE_LNG", "0"))
BASE_FEE = float(os.getenv("DELIVERY_BASE_FEE", "0"))       # e.g. 50
PER_KM   = float(os.getenv("DELIVERY_PER_KM", "0"))         # e.g. 6
FREE_RADIUS_KM = float(os.getenv("FREE_RADIUS_KM", "0"))    # e.g. 5

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lng points in kilometers."""
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    return 2 * R * asin(sqrt(a))

def compute_delivery_fee_km(dist_km: float,
                            base_fee: float = BASE_FEE,
                            per_km: float = PER_KM,
                            free_radius: float = FREE_RADIUS_KM) -> float:
    """
    Fee = base_fee + max(0, dist - free_radius) * per_km
    Rounded to 2 decimals.
    """
    extra = max(0.0, float(dist_km) - float(free_radius))
    return round(float(base_fee) + extra * float(per_km), 2)

def ensure_delivery_computed(user) -> None:
    """
    Make sure the user's distance_km and delivery_fee are computed & saved
    if we already know their lat/lng. No-op if lat/lng missing.
    """
    try:
        lat = float(user.lat) if user.lat is not None else None
        lng = float(user.lng) if user.lng is not None else None
    except Exception:
        lat = lng = None

    if lat is None or lng is None:
        return  # address not saved yet

    needs = (user.distance_km is None) or (user.delivery_fee is None)
    if not needs:
        return

    dist_km = haversine_km(BASE_LAT, BASE_LNG, lat, lng)
    fee = compute_delivery_fee_km(dist_km)
    user.distance_km = dist_km
    user.delivery_fee = fee
    db.session.commit()


# --------------------------
# External keys & knobs
# --------------------------
OPENAI_API_KEY           = os.getenv("OPENAI_API_KEY", "")
WIPAY_API_KEY            = os.getenv("WIPAY_API_KEY", "")
GOOGLE_MAPS_BROWSER_KEY  = os.getenv("GOOGLE_MAPS_BROWSER_KEY", "")
GOOGLE_MAPS_SERVER_KEY   = os.getenv("GOOGLE_MAPS_SERVER_KEY", "")

def _f(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)

BASE_LAT  = _f(os.getenv("BASE_LAT", 0), 0)
BASE_LNG  = _f(os.getenv("BASE_LNG", 0), 0)
DELIVERY_BASE_FEE = _f(os.getenv("DELIVERY_BASE_FEE", 50), 50)
DELIVERY_PER_KM   = _f(os.getenv("DELIVERY_PER_KM", 5), 5)

# Inject shared values into templates
@app.context_processor
def inject_globals():
    return {
        "ts": int(_time.time()),
        "GOOGLE_MAPS_API_KEY": os.getenv("GOOGLE_MAPS_API_KEY", ""),
        "BASE_LAT": float(os.getenv("BASE_LAT", "0") or 0),
        "BASE_LNG": float(os.getenv("BASE_LNG", "0") or 0),
    }

# --------------------------
# Models
# --------------------------
class User(db.Model, UserMixin):
    id                 = db.Column(db.Integer, primary_key=True)
    username           = db.Column(db.String(150), unique=True, nullable=False)
    email              = db.Column(db.String(150), unique=True, nullable=False)
    password           = db.Column(db.String(150), nullable=False)
    address            = db.Column(db.String(300), nullable=False)
    age                = db.Column(db.Integer, nullable=False)
    # Google/Delivery extras (nullable for backward compat)
    place_id           = db.Column(db.String(100), nullable=True)
    formatted_address  = db.Column(db.String(300), nullable=True)
    lat                = db.Column(db.Float, nullable=True)
    lng                = db.Column(db.Float, nullable=True)
    distance_km        = db.Column(db.Float, nullable=True)
    delivery_fee       = db.Column(db.Float, nullable=True)
    created_at         = db.Column(db.DateTime, default=datetime.utcnow)

class Order(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    product_name = db.Column(db.String(150), nullable=False)
    amount       = db.Column(db.Float, nullable=False)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --------------------------
# BuildAdvisor imports (defensive)
# --------------------------
_BA_IMPORT_ERROR = None
try:
    from loaders import load_aggregates, load_steel, load_building, merge_prices
except Exception as e:
    _BA_IMPORT_ERROR = f"Import error in loaders: {e}"
    log.exception(_BA_IMPORT_ERROR)

try:
    from ai_text import propose_bom_with_ai, expand_steps_with_ai, propose_bom_from_vision
except Exception as e:
    _BA_IMPORT_ERROR = (_BA_IMPORT_ERROR + " | " if _BA_IMPORT_ERROR else "") + f"Import error in ai_text: {e}"
    log.exception("AI import error", exc_info=True)

# --------------------------
# CSV path resolution
# --------------------------
DATA_DIR = os.getenv("DATA_DIR", "data")

def _resolve_csv(env_name: str, default_filename: str) -> str:
    val = os.getenv(env_name)
    if val:
        if os.path.isabs(val) or any(sep in val for sep in ("/", "\\")):
            return val
        return os.path.join(DATA_DIR, val)
    return os.path.join(DATA_DIR, default_filename)

AGGREGATES_CSV = _resolve_csv("AGGREGATES_CSV", "buildadvisor_aggregates.csv")
BUILDING_CSV   = _resolve_csv("BUILDING_CSV",   "building materials.csv")
STEEL_CSV      = _resolve_csv("STEEL_CSV",      "steel.csv")
LUMBER_CSV     = _resolve_csv("LUMBER_CSV",     "lumber.csv")

# --------------------------
# Price loading
# --------------------------
def _safe_load_prices():
    if _BA_IMPORT_ERROR:
        return {}, {}, _BA_IMPORT_ERROR
    try:
        agg = load_aggregates(AGGREGATES_CSV)
        steel_prices, steel_rows = load_steel(STEEL_CSV)
        building_prices, b_meta = load_building(BUILDING_CSV)
        prices = merge_prices(agg, steel_prices, building_prices)
        meta = {"steel_rows": steel_rows, "building_meta": b_meta, "aggregates": agg}
        return prices, meta, None
    except Exception as e:
        log.exception("Failed to load price files")
        return {}, {}, f"Failed to load price files: {e}"

PRICES, META, PRICES_ERROR = _safe_load_prices()

# --------------------------
# WiPay helper
# --------------------------
def create_wipay_payment(amount: float, order_id: int) -> str:
    """
    Calls WiPay to create a hosted payment and returns the payment URL.
    Raises an exception on failure.
    """
    if not WIPAY_API_KEY:
        raise RuntimeError("WIPAY_API_KEY is missing")

    payload = {
        "order_id": str(order_id or 0),
        "amount": float(amount),
        "currency": "TTD",
        "redirect_url": url_for("payment_success", _external=True),
        "callback_url": url_for("payment_callback", _external=True),
    }
    headers = {"Authorization": f"Bearer {WIPAY_API_KEY}"}

    resp = requests.post(
        "https://sandbox-api.wipayfinancial.com/v1/payments",
        json=payload, headers=headers, timeout=20
    )

    if resp.status_code == 200 and resp.headers.get("content-type","").startswith("application/json"):
        url = (resp.json() or {}).get("payment_url")
        if url:
            return url

    raise RuntimeError(f"WiPay error {resp.status_code}: {resp.text[:400]}")

# --------------------------
# Pricing helpers
# --------------------------
ALLOWED_KEYS = {
    "sand_m3","sharp_sand_m3","gravel_m3","red_sand_m3","backfill_m3","soakaway_boulders_m3",
    "cement_bag","cement_bag_eco","cement_bag_premium","cement_loose_lb",
    "block_4in","block_6in","block_8in","block_clay_4in",
    "rebar_corr_3_8_m","rebar_corr_1_2_m","rebar_corr_5_8_m",
    "rebar_mild_3_8_m","rebar_mild_1_2_m","rebar_mild_5_8_m",
    "mesh_A142_sheet","tie_wire_kg","purlin_z_m","purlin_c_m","paint_gal"
}

def pretty_name(key: str) -> str:
    if key.endswith("_m3"):    return key.replace("_m3"," (m³)").replace("_"," ")
    if key.endswith("_m"):     return key.replace("_m"," (m)").replace("_"," ")
    if key.endswith("_gal"):   return key.replace("_gal"," (gal)").replace("_"," ")
    if key.endswith("_bag"):   return key.replace("_bag"," (bag)").replace("_"," ")
    if key.endswith("_sheet"): return key.replace("_sheet"," (sheet)").replace("_"," ")
    if key.endswith("_kg"):    return key.replace("_kg"," (kg)").replace("_"," ")
    return key.replace("_"," ")

def price_bom_lines(lines, prices):
    out_lines = []
    total = 0.0

    def _lookup_price(key):
        if key == "cement_bag":
            for alt in ("cement_bag", "cement_bag_eco", "cement_bag_premium"):
                if prices.get(alt) is not None:
                    return prices[alt]
            return None
        return prices.get(key)

    for it in (lines or []):
        key = it.get("key"); qty = float(it.get("qty", 0) or 0); unit = it.get("unit")
        if key not in ALLOWED_KEYS:
            continue
        up = _lookup_price(key)
        if up is None:
            out_lines.append({"name": pretty_name(key) + " — UNPRICED", "qty": qty, "unit": unit, "unit_price": 0, "total": 0})
            continue
        line_total = qty * float(up)
        total += line_total
        out_lines.append({"name": pretty_name(key), "qty": round(qty,2), "unit": unit, "unit_price": round(float(up),2), "total": round(line_total,2)})
    return {"lines": out_lines, "total": round(total,2)}

# --------------------------
# API-only headers & errors
# --------------------------
@app.after_request
def add_no_cache_headers(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp

@app.errorhandler(404)
def handle_404(e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "Not found"}), 404
    tpl = os.path.join("templates","404.html")
    return (render_template("404.html"), 404) if os.path.exists(tpl) else ("Not found", 404)

@app.errorhandler(500)
def handle_500(e):
    log.exception("Unhandled error")
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "Server error"}), 500
    tpl = os.path.join("templates","500.html")
    return (render_template("500.html"), 500) if os.path.exists(tpl) else ("Server error", 500)

# --------------------------
# File serving (uploads)
# --------------------------
@app.route("/uploads/<path:filename>")
def serve_upload(filename: str):
    # Note: do not list directories; only serve files inside UPLOAD_FOLDER
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename, as_attachment=False)

# --------------------------
# Auth helpers & routes
# --------------------------
email_re = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def is_valid_email(e: str) -> bool:
    return bool(e and email_re.match(e))

def is_safe_url(target: str) -> bool:
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target or ""))
    return test_url.scheme in ("http", "https") and ref_url.netloc == test_url.netloc

@login_manager.unauthorized_handler
def _unauth():
    flash("Please log in to continue.", "warning")
    return redirect(url_for("login", next=request.full_path))

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        flash("You’re already signed in.", "info")
        return redirect(url_for("index"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        email    = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        confirm  = request.form.get("confirm") or request.form.get("password2") or ""
        address  = (request.form.get("address") or "").strip()
        age_raw  = (request.form.get("age") or "").strip()

        # From hidden fields (from signup.html)
        place_id          = (request.form.get("place_id") or "").strip()
        formatted_address = (request.form.get("formatted_address") or "").strip()
        lat_raw           = request.form.get("lat")
        lng_raw           = request.form.get("lng")
        dist_raw          = request.form.get("distance_km")
        fee_raw           = request.form.get("delivery_fee")

        errors = []
        if len(username) < 3:
            errors.append("Username must be at least 3 characters.")
        if not is_valid_email(email):
            errors.append("Please enter a valid email address.")
        if len(password) < 6:
            errors.append("Password must be at least 6 characters.")
        if password != confirm:
            errors.append("Passwords do not match.")
        try:
            age = int(age_raw)
            if age < 13:
                errors.append("You must be at least 13 years old.")
        except ValueError:
            errors.append("Age must be a number.")

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("signup.html", form=request.form)

        if User.query.filter_by(email=email).first():
            flash("That email is already registered.", "danger")
            return render_template("signup.html", form=request.form)
        if User.query.filter_by(username=username).first():
            flash("That username is taken.", "danger")
            return render_template("signup.html", form=request.form)

        try:
            lat = float(lat_raw) if lat_raw not in (None, "",) else None
            lng = float(lng_raw) if lng_raw not in (None, "",) else None
        except ValueError:
            lat, lng = None, None

        try:
            distance_km = float(dist_raw) if dist_raw not in (None, "",) else None
            delivery_fee = float(fee_raw) if fee_raw not in (None, "",) else None
        except ValueError:
            distance_km, delivery_fee = None, None

        try:
            hashed = bcrypt.generate_password_hash(password).decode("utf-8")
            user = User(
                username=username, email=email, password=hashed,
                address=address, age=age,
                place_id=place_id or None,
                formatted_address=formatted_address or address,
                lat=lat, lng=lng,
                distance_km=distance_km, delivery_fee=delivery_fee
            )
            db.session.add(user)
            db.session.commit()
            flash("Account created! Please log in.", "success")
            return redirect(url_for("login"))
        except IntegrityError:
            db.session.rollback()
            flash("That email or username is already in use.", "danger")
        except Exception as ex:
            db.session.rollback()
            log.exception("Signup failed")
            flash(f"Signup failed: {ex}", "danger")

    return render_template("signup.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        flash("You’re already signed in.", "info")
        return redirect(url_for("index"))

    next_url = request.args.get("next")
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        remember = bool(request.form.get("remember"))
        dest = request.form.get("next") or next_url

        user = User.query.filter_by(email=email).first()
        if user and bcrypt.check_password_hash(user.password, password):
            login_user(user, remember=remember)
            flash("Welcome back!", "success")
            if dest and is_safe_url(dest):
                return redirect(dest)
            return redirect(url_for("index"))
        flash("Invalid email or password.", "danger")

    return render_template("login.html", next=next_url or "")

@app.route("/logout", methods=["GET", "POST"])
@login_required
def logout():
    logout_user()
    flash("You’ve been logged out.", "info")
    return redirect(url_for("login"))

# --------------------------
# Distance & Delivery quote API
# --------------------------
def distance_km_from_base(dest_lat: float, dest_lng: float):
    """
    Returns distance in km between (BASE_LAT, BASE_LNG) and (dest_lat, dest_lng)
    using Google Distance Matrix (server key). None on failure.
    """
    try:
        r = requests.get(
            "https://maps.googleapis.com/maps/api/distancematrix/json",
            params={
                "origins": f"{BASE_LAT},{BASE_LNG}",
                "destinations": f"{dest_lat},{dest_lng}",
                "units": "metric",
                "key": GOOGLE_MAPS_SERVER_KEY
            },
            timeout=15
        )
        data = r.json()
        meters = data["rows"][0]["elements"][0]["distance"]["value"]
        return meters / 1000.0
    except Exception:
        return None

@app.post("/api/delivery-quote")
def delivery_quote():
    data = request.get_json(silent=True) or {}
    try:
        lat = float(data.get("lat"))
        lng = float(data.get("lng"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid lat/lng"}), 400

    km = distance_km_from_base(lat, lng)
    if km is None:
        return jsonify({"ok": False, "error": "Could not compute distance"}), 502

    fee = round(DELIVERY_BASE_FEE + DELIVERY_PER_KM * km, 2)
    return jsonify({"ok": True, "distance_km": round(km, 2), "fee": fee})

# --------------------------
# Core site routes
# --------------------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/products")
def products():
    return render_template("products.html")

@app.route("/upload")
def upload_page():
    return render_template("upload.html")

# --------------------------
# Upload API
# --------------------------
@app.post("/api/uploads")
def api_uploads():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file part"}), 400

    files = request.files.getlist("file")
    if not files:
        return jsonify({"ok": False, "error": "No files uploaded"}), 400

    saved = []
    for f in files:
        if not f or not f.filename:
            continue
        if not _allowed_file(f.filename):
            return jsonify({"ok": False, "error": f"Unsupported file type: {f.filename}"}), 400
        fname = secure_filename(f.filename)
        # Disambiguate duplicate names by prefixing timestamp
        unique_name = f"{int(_time.time()*1000)}_{fname}"
        path = os.path.join(app.config["UPLOAD_FOLDER"], unique_name)
        f.save(path)

        ext = unique_name.rsplit(".", 1)[-1].lower()
        saved.append({
            "id": unique_name,
            "filename": unique_name,
            "ext": ext,
            "url": url_for("serve_upload", filename=unique_name, _external=False)
        })

    if not saved:
        return jsonify({"ok": False, "error": "Nothing saved"}), 400

    return jsonify({"ok": True, "files": saved})

# Optional legacy address verification page
@app.route("/verify-address", methods=["GET", "POST"])
def verify_address():
    if request.method == "POST":
        address = request.form.get("address", "")
        log.info("Address received: %s", address)
        try:
            resp = requests.get(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"address": address, "key": GOOGLE_MAPS_SERVER_KEY},
                timeout=15
            )
            data = resp.json()
            if resp.status_code == 200 and data.get("results"):
                loc = data["results"][0]["geometry"]["location"]
                formatted = data["results"][0]["formatted_address"]
                return render_template("verify_address.html", address=formatted, lat=loc["lat"], lng=loc["lng"])
        except Exception as ex:
            log.exception("Google Maps verify failed")
            flash(f"Address verification error: {ex}", "danger")
        flash("Invalid address. Please try again.", "danger")
        return redirect(url_for("verify_address"))
    return render_template("verify_address.html")

# --------------------------
# Checkout / Payment
# --------------------------
@app.route("/checkout", methods=["POST"])
@login_required
def checkout():
    """
    Expects JSON cart [{ productName, price, quantity }, ...]
    Creates Order rows, totals the amount, calls WiPay, and returns {ok, payment_url}.
    """
    try:
        cart = request.get_json(force=True, silent=False)
    except Exception as ex:
        return jsonify({"ok": False, "error": f"Invalid JSON: {ex}"}), 400

    if not isinstance(cart, list) or not cart:
        return jsonify({"ok": False, "error": "Your cart is empty."}), 400

    try:
        grand_total = 0.0
        last_order = None

        for item in cart:
            name = (item.get("productName") or "").strip()
            price = float(item.get("price") or 0)
            qty   = int(item.get("quantity") or 0)
            if not name or price <= 0 or qty <= 0:
                continue
            line_total = price * qty
            grand_total += line_total

            last_order = Order(
                user_id=current_user.id,
                product_name=name,
                amount=line_total
            )
            db.session.add(last_order)

        if grand_total <= 0:
            return jsonify({"ok": False, "error": "No payable items in your cart."}), 400

        db.session.commit()

        payment_url = create_wipay_payment(grand_total, last_order.id if last_order else 0)
        return jsonify({"ok": True, "payment_url": payment_url})

    except Exception as ex:
        db.session.rollback()
        log.exception("Checkout failed")
        return jsonify({"ok": False, "error": str(ex)}), 500

@app.route("/payment", methods=["GET", "POST"])
@login_required
def payment():
    """
    GET  -> render the Payment page (ensures delivery quote is computed)
    POST -> two supported forms:
        - JSON body (cart checkout from payment.html) -> returns JSON {ok, payment_url}
        - Form POST (legacy single-item checkout)     -> redirects to WiPay
    """
    # --- Always try to ensure we have delivery distance/fee on hand ---
    try:
        ensure_delivery_computed(current_user)
    except Exception:
        app.logger.exception("ensure_delivery_computed() failed in /payment")

    # -------------------------
    # RENDER (GET)
    # -------------------------
    if request.method == "GET":
        # We pass nothing special; your payment.html fetches the quote via /api/me/delivery-quote
        return render_template("payment.html")

    # -------------------------
    # CHARGE (POST)
    # -------------------------
    # If the page JS posts JSON (cart checkout)
    if request.is_json:
        try:
            cart = request.get_json(force=True, silent=False)
        except Exception as ex:
            return jsonify({"ok": False, "error": f"Invalid JSON: {ex}"}), 400

        if not isinstance(cart, list) or not cart:
            return jsonify({"ok": False, "error": "Your cart is empty."}), 400

        try:
            grand_total = 0.0
            last_order = None

            for item in cart:
                name = (item.get("productName") or "").strip()
                price = float(item.get("price") or 0)
                qty   = int(item.get("quantity") or 0)
                if not name or price <= 0 or qty <= 0:
                    continue

                line_total = price * qty
                grand_total += line_total

                last_order = Order(
                    user_id=current_user.id,
                    product_name=name,
                    amount=line_total
                )
                db.session.add(last_order)

            # Add delivery fee if we have one (your UI already adds it to grand total;
            # this makes the server authoritative & aligns WiPay total with what user sees)
            delivery_fee = float(current_user.delivery_fee or 0.0)
            if delivery_fee > 0:
                last_order = Order(
                    user_id=current_user.id,
                    product_name="Delivery",
                    amount=delivery_fee
                )
                db.session.add(last_order)
                grand_total += delivery_fee

            if grand_total <= 0:
                return jsonify({"ok": False, "error": "No payable items in your cart."}), 400

            db.session.commit()

            # Call WiPay (hosted checkout)
            payment_url = create_wipay_payment(grand_total, last_order.id if last_order else 0)
            return jsonify({"ok": True, "payment_url": payment_url})

        except Exception as ex:
            db.session.rollback()
            app.logger.exception("JSON checkout in /payment failed")
            return jsonify({"ok": False, "error": str(ex)}), 500

    # Otherwise treat as legacy form POST (single item)
    try:
        product_name = (request.form.get("product_name") or "").strip()
        amount = float(request.form.get("amount") or 0)
        if not product_name or amount <= 0:
            flash("Invalid product or amount.", "danger")
            return render_template("payment.html")

        order = Order(user_id=current_user.id, product_name=product_name, amount=amount)
        db.session.add(order)

        # Legacy form did not include delivery; if you want to add it here too:
        delivery_fee = float(current_user.delivery_fee or 0.0)
        if delivery_fee > 0:
            db.session.add(Order(
                user_id=current_user.id,
                product_name="Delivery",
                amount=delivery_fee
            ))
            amount += delivery_fee  # charge the combined amount

        db.session.commit()

        # Hosted checkout at WiPay
        payment_url = create_wipay_payment(amount, order.id)
        return redirect(payment_url)

    except Exception as ex:
        db.session.rollback()
        app.logger.exception("Form checkout in /payment failed")
        flash(f"Payment error: {ex}", "danger")
        return render_template("payment.html")


@app.route("/payment-callback", methods=["POST"])
def payment_callback():
    try:
        data = request.get_json(force=True, silent=False)
        log.info("Payment callback received: %s", json.dumps(data)[:1000])
        # TODO: verify signature / update order status here
        return jsonify({"status": "success"}), 200
    except Exception as ex:
        log.exception("Callback parse error")
        return jsonify({"status": "error", "detail": str(ex)}), 400

@app.route("/payment-success")
def payment_success():
    flash("Payment successful!", "success")
    return render_template("success.html")

# --------------------------
# BuildAdvisor API & page
# --------------------------
@app.route("/health")
def health():
    files = {
        "aggregates": os.path.abspath(AGGREGATES_CSV),
        "building":   os.path.abspath(BUILDING_CSV),
        "lumber":     os.path.abspath(LUMBER_CSV),
        "steel":      os.path.abspath(STEEL_CSV),
    }
    exists = {k: os.path.exists(v) for k, v in files.items()}
    return jsonify({
        "ok": True,
        "has_openai": bool(OPENAI_API_KEY),
        "data_files": files,
        "exists": exists,
        "price_keys": len(PRICES),
        "import_error": _BA_IMPORT_ERROR,
        "prices_error": PRICES_ERROR,
    })

@app.route("/api/chat", methods=["POST"])
def api_chat():
    # Always return JSON—even on errors
    try:
        try:
            body = request.get_json(force=True)
        except Exception as ex:
            return jsonify({"ok": False, "error": f"Invalid JSON: {ex}"}), 400

        msg = (body.get("message") or "").strip()
        spec = body.get("spec") or {}
        if not msg:
            return jsonify({"ok": False, "error": "Empty message"}), 400

        if _BA_IMPORT_ERROR:
            return jsonify({"ok": False, "error": _BA_IMPORT_ERROR}), 500
        if PRICES_ERROR:
            return jsonify({"ok": False, "error": PRICES_ERROR}), 500
        if not OPENAI_API_KEY:
            return jsonify({"ok": False, "error": "OPENAI_API_KEY is not set"}), 500

        ai_bom = propose_bom_with_ai(msg, spec)
        if not isinstance(ai_bom, dict) or not isinstance(ai_bom.get("lines"), list):
            raise TypeError("propose_bom_with_ai must return a dict with 'lines' list")

        priced = price_bom_lines(ai_bom["lines"], PRICES)
        default_text = "Here’s the step-by-step plan and a materials summary."
        narrative = expand_steps_with_ai(msg, spec, priced, default_text)
        if not isinstance(narrative, str):
            narrative = default_text

        return jsonify({
            "ok": True,
            "assistant": narrative,
            "spec": spec,
            "estimate": priced,
            "ai_notes": ai_bom.get("notes", "")
        })
    except Exception as ex:
        log.exception("api_chat failed")
        return jsonify({
            "ok": False,
            "error": f"{type(ex).__name__}: {ex}",
            "trace": traceback.format_exc(limit=2)
        }), 500

@app.route("/buildadvisor")
def buildadvisor():
    return render_template("buildadvisor.html")

# --------------------------
# BOM extraction from uploads (Vision)
# --------------------------
@app.post("/api/bom/extract")
def api_bom_extract():
    try:
        body = request.get_json(force=True, silent=False) or {}
    except Exception as ex:
        return jsonify({"ok": False, "error": f"Invalid JSON: {ex}"}), 400

    file_ids = body.get("file_ids") or []
    spec = body.get("spec") or {}
    if not isinstance(file_ids, list) or not file_ids:
        return jsonify({"ok": False, "error": "file_ids must be a non-empty list"}), 400

    if _BA_IMPORT_ERROR:
        return jsonify({"ok": False, "error": _BA_IMPORT_ERROR}), 500
    if PRICES_ERROR:
        return jsonify({"ok": False, "error": PRICES_ERROR}), 500
    if not OPENAI_API_KEY:
        return jsonify({"ok": False, "error": "OPENAI_API_KEY is not set"}), 500

    # Resolve paths inside UPLOAD_FOLDER and ensure files exist
    paths = []
    for fid in file_ids:
        # Prevent escaping upload dir
        fname = secure_filename(str(fid))
        path = os.path.join(app.config["UPLOAD_FOLDER"], fname)
        if not os.path.isfile(path):
            return jsonify({"ok": False, "error": f"File not found: {fid}"}), 400
        paths.append(path)

    ai_bom = propose_bom_from_vision(paths, spec)
    if not isinstance(ai_bom, dict) or not isinstance(ai_bom.get("lines"), list):
        return jsonify({"ok": False, "error": "Vision extraction failed"}), 502

    priced = price_bom_lines(ai_bom["lines"], PRICES)
    default_text = "Here’s the step-by-step plan and a materials summary."
    narrative = expand_steps_with_ai("Document analysis", spec, priced, default_text)
    if not isinstance(narrative, str):
        narrative = default_text

    return jsonify({
        "ok": True,
        "assistant": narrative,
        "spec": spec,
        "estimate": priced,
        "ai_notes": ai_bom.get("notes", "")
    })

@app.post("/api/me/recompute")
@login_required
def me_recompute():
    if current_user.lat is None or current_user.lng is None:
        return jsonify({"ok": False, "error": "No saved lat/lng"}), 400

    dist_km = float(haversine_km(BASE_LAT, BASE_LNG, current_user.lat, current_user.lng))
    fee = float(compute_delivery_fee_km(dist_km))

    current_user.distance_km = dist_km
    current_user.delivery_fee = fee
    db.session.commit()
    return jsonify({"ok": True, "distance_km": dist_km, "delivery_fee": fee})


@app.post("/api/me/location")
@login_required
def me_location():
    data = request.get_json(force=True) or {}
    try:
        lat = float(data.get("lat"))
        lng = float(data.get("lng"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "lat/lng required"}), 400

    addr = (data.get("formatted_address") or "").strip() or None
    dist_km = float(haversine_km(BASE_LAT, BASE_LNG, lat, lng))
    fee = float(compute_delivery_fee_km(dist_km))

    current_user.place_id = (data.get("place_id") or None)
    current_user.formatted_address = addr
    current_user.lat = lat
    current_user.lng = lng
    current_user.distance_km = dist_km
    current_user.delivery_fee = fee

    db.session.commit()
    return jsonify({"ok": True, "distance_km": dist_km, "delivery_fee": fee})

# --------------------------
# Entrypoint
# --------------------------


@app.route("/me/debug")
@login_required
def me_debug():
    u = current_user
    return jsonify({
        "place_id": u.place_id,
        "formatted_address": u.formatted_address,
        "lat": u.lat,
        "lng": u.lng,
        "distance_km": u.distance_km,
        "delivery_fee": u.delivery_fee
    })
    
    
if __name__ == "__main__":
    with app.app_context():
        db.create_all()  # adds new nullable columns if they don't exist (SQLite will append)
    # Bind to 0.0.0.0 and respect PORT for hosting platforms (e.g., Railway)
    port = int(os.getenv("PORT", "5000"))
    host = os.getenv("HOST", "0.0.0.0")
    debug = str(os.getenv("FLASK_DEBUG", "1")).lower() in ("1", "true", "yes")
    app.run(host=host, port=port, debug=debug)
