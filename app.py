# app.py
import os
import re
import json
import time as _time
import logging
import traceback
from datetime import datetime, timedelta
from functools import wraps
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
    # Staff access flag (added via schema helper if missing)
    is_staff           = db.Column(db.Boolean, nullable=False, default=False)

class Order(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    user_id      = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    product_name = db.Column(db.String(150), nullable=False)
    amount       = db.Column(db.Float, nullable=False)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

# --------------------------
# Staff ERP Models
# --------------------------

class Supplier(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(200), nullable=False)
    phone      = db.Column(db.String(100), nullable=True)
    email      = db.Column(db.String(200), nullable=True)
    address    = db.Column(db.String(400), nullable=True)
    tax_id     = db.Column(db.String(100), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    invoices   = db.relationship("PurchaseInvoice", backref="supplier", lazy=True)

class PurchaseInvoice(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    supplier_id     = db.Column(db.Integer, db.ForeignKey("supplier.id"), nullable=True)
    supplier_name   = db.Column(db.String(200), nullable=True)  # fallback if supplier not in table yet
    invoice_date    = db.Column(db.String(40), nullable=True)
    invoice_date_dt = db.Column(db.Date, nullable=True)
    invoice_number  = db.Column(db.String(120), nullable=True)
    currency        = db.Column(db.String(10), nullable=True, default="TTD")
    subtotal        = db.Column(db.Float, nullable=True)
    tax             = db.Column(db.Float, nullable=True)
    total           = db.Column(db.Float, nullable=True)
    status          = db.Column(db.String(20), nullable=False, default="draft")  # draft|posted
    uploaded_files  = db.Column(db.Text, nullable=True)  # JSON string list of file ids
    created_by      = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    lines           = db.relationship("PurchaseLineItem", backref="invoice", lazy=True)

class PurchaseLineItem(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    invoice_id  = db.Column(db.Integer, db.ForeignKey("purchase_invoice.id"), nullable=False)
    description = db.Column(db.String(300), nullable=False)
    category    = db.Column(db.String(100), nullable=True)
    material_key= db.Column(db.String(100), nullable=True)
    unit        = db.Column(db.String(20), nullable=False)  # yd3, m3, bag, etc.
    quantity    = db.Column(db.Float, nullable=False)
    unit_price  = db.Column(db.Float, nullable=True)
    line_total  = db.Column(db.Float, nullable=True)

class SalesReceipt(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    receipt_no    = db.Column(db.String(50), nullable=True)
    customer_name = db.Column(db.String(200), nullable=True)
    created_by    = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    subtotal      = db.Column(db.Float, nullable=True)
    tax           = db.Column(db.Float, nullable=True)
    total         = db.Column(db.Float, nullable=True)
    notes         = db.Column(db.String(300), nullable=True)
    lines         = db.relationship("SalesReceiptLine", backref="receipt", lazy=True)

class SalesReceiptLine(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    receipt_id  = db.Column(db.Integer, db.ForeignKey("sales_receipt.id"), nullable=False)
    item_name   = db.Column(db.String(200), nullable=False)
    material_key= db.Column(db.String(100), nullable=True)
    unit        = db.Column(db.String(20), nullable=False, default="yd3")
    quantity    = db.Column(db.Float, nullable=False)
    unit_price  = db.Column(db.Float, nullable=False)
    line_total  = db.Column(db.Float, nullable=False)

# --------------------------
# Expenses model
# --------------------------

class Expense(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    date        = db.Column(db.String(40), nullable=True)
    date_dt     = db.Column(db.Date, nullable=True)
    category    = db.Column(db.String(50), nullable=False)  # salaries, fuel, maintenance, other
    description = db.Column(db.String(300), nullable=True)
    amount      = db.Column(db.Float, nullable=False)
    created_by  = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

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
    from ai_text import (
        propose_bom_with_ai,
        expand_steps_with_ai,
        propose_bom_from_vision,
        propose_invoice_from_vision,
        propose_purchase_from_text,
        propose_expenses_from_text,
        propose_expenses_from_vision,
    )
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

# --------------------------
# Staff auth helper
# --------------------------

def staff_required(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args, **kwargs):
        if not getattr(current_user, "is_staff", False):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Forbidden"}), 403
            flash("Staff access required.", "danger")
            return redirect(url_for("index"))
        return fn(*args, **kwargs)
    return wrapper

# --------------------------
# Staff report helpers & unit conversions
# --------------------------

# Bag constants (1 yd³ = 20 bags; 1 bag = 0.05 yd³)
BAGS_PER_YD3 = int(os.getenv("BAGS_PER_YD3", "20")) or 20
BAG_TO_YD3 = 1.0 / float(BAGS_PER_YD3)
BAG_COST_PER_BAG = float(os.getenv("BAG_COST_PER_BAG", "2"))

def to_yd3(quantity: float, unit: str, product: str | None = None) -> float:
    try:
        qv = float(quantity or 0)
    except Exception:
        return 0.0
    u = (unit or "").strip().lower()
    if u == "yd3":
        return qv
    if u == "m3":
        # 1 m3 = 0.764555 yd3
        return qv / 0.764555
    if u in ("bag", "bags"):
        # Only treat bags as yd3 for aggregates we recognize
        prod = normalize_material_name(product or "")
        if prod in ("sand", "gravel", "sharp_sand"):
            return qv * BAG_TO_YD3
        return 0.0
    return 0.0


def normalize_material_name(name: str) -> str:
    n = (name or "").strip().lower()
    if not n:
        return "other"
    # prefer exact keys
    if any(k in n for k in ("sharp sand", "sharp_sand")):
        return "sharp_sand"
    if "gravel" in n:
        return "gravel"
    if "sand" in n:
        return "sand"
    return "other"


def _compute_weighted_avg_cost(from_date: str | None, to_date: str | None) -> dict:
    """Return { product: avg_cost_per_yd3 } based on purchases in date range."""
    q = db.session.query(PurchaseInvoice, PurchaseLineItem).join(
        PurchaseLineItem, PurchaseLineItem.invoice_id == PurchaseInvoice.id
    )
    if from_date:
        q = q.filter((PurchaseInvoice.invoice_date_dt >= from_date) | (PurchaseInvoice.created_at >= from_date))
    if to_date:
        q = q.filter((PurchaseInvoice.invoice_date_dt <= to_date) | (PurchaseInvoice.created_at <= to_date))

    totals: dict[str, dict] = {}
    rows = q.all()
    for inv, li in rows:
        # Determine product name
        key_src = (li.material_key or li.category or li.description or "").strip()
        product = normalize_material_name(key_src)
        if product == "other":
            # Skip non-aggregate lines for v1 cost
            continue
        qty_yd3 = to_yd3(li.quantity, li.unit, product)
        if qty_yd3 <= 0:
            continue
        line_cost = li.line_total
        if line_cost is None:
            try:
                line_cost = (li.unit_price or 0.0) * float(li.quantity or 0.0)
            except Exception:
                line_cost = 0.0
        try:
            line_cost = float(line_cost or 0.0)
        except Exception:
            line_cost = 0.0

        if product not in totals:
            totals[product] = {"qty_yd3": 0.0, "cost": 0.0}
        totals[product]["qty_yd3"] += qty_yd3
        totals[product]["cost"] += line_cost

    avg: dict[str, float] = {}
    for p, t in totals.items():
        if t["qty_yd3"] > 0:
            avg[p] = t["cost"] / t["qty_yd3"]
    return avg

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
        is_staff_flag = True if request.form.get("is_staff") else False

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
                distance_km=distance_km, delivery_fee=delivery_fee,
                is_staff=bool(is_staff_flag)
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
# Staff pages (UI)
# --------------------------

@app.get("/staff/purchases")
@staff_required
def staff_purchases_list():
    # Simple latest-first list; filters can be added later
    invoices = PurchaseInvoice.query.order_by(PurchaseInvoice.created_at.desc()).limit(200).all()
    return render_template("staff/purchases_list.html", invoices=invoices)


@app.get("/staff/purchases/new")
@staff_required
def staff_purchase_new():
    inv_id = request.args.get("id")
    invoice_data = None
    if inv_id:
        try:
            inv = PurchaseInvoice.query.get(int(inv_id))
        except Exception:
            inv = None
        if inv:
            lines = PurchaseLineItem.query.filter_by(invoice_id=inv.id).all()
            invoice_data = {
                "id": inv.id,
                "supplier_name": inv.supplier_name or (inv.supplier.name if getattr(inv, "supplier", None) else None),
                "invoice_number": inv.invoice_number,
                "invoice_date": inv.invoice_date,
                "currency": inv.currency,
                "subtotal": inv.subtotal,
                "tax": inv.tax,
                "total": inv.total,
                "status": inv.status,
                "lines": [
                    {
                        "id": li.id,
                        "description": li.description,
                        "category": li.category,
                        "material_key": li.material_key,
                        "unit": li.unit,
                        "qty": li.quantity,
                        "unit_price": li.unit_price,
                        "line_total": li.line_total,
                    }
                    for li in lines
                ]
            }
    return render_template("staff/purchase_edit.html", invoice=invoice_data)


@app.get("/staff/billing")
@staff_required
def staff_billing():
    return render_template("staff/billing.html")


@app.get("/staff/reports/purchases")
@staff_required
def staff_reports_purchases():
    """Aggregate purchase volumes (in yd3) and costs by material and supplier.
    Supports CSV via ?format=csv&from=YYYY-MM-DD&to=YYYY-MM-DD
    """
    # Filters
    from_date = (request.args.get("from") or "").strip() or None
    to_date = (request.args.get("to") or "").strip() or None
    fmt = (request.args.get("format") or "").strip().lower()

    q = db.session.query(PurchaseInvoice, PurchaseLineItem).join(PurchaseLineItem, PurchaseLineItem.invoice_id == PurchaseInvoice.id)
    if from_date:
        q = q.filter((PurchaseInvoice.invoice_date_dt >= from_date) | (PurchaseInvoice.created_at >= from_date))
    if to_date:
        q = q.filter((PurchaseInvoice.invoice_date_dt <= to_date) | (PurchaseInvoice.created_at <= to_date))

    def to_yd3(quantity: float, unit: str) -> float:
        try:
            qv = float(quantity or 0)
        except Exception:
            return 0.0
        u = (unit or "").strip().lower()
        if u == "yd3":
            return qv
        if u == "m3":
            # 1 m3 = 1 / 0.764555 yd3 ≈ 1.308
            return qv / 0.764555
        # ignore non-volume units in yd3 totals
        return 0.0

    # Aggregate in Python for clarity
    rows = q.all()
    agg = {}
    for inv, li in rows:
        material = (li.material_key or li.category or li.description or "Unknown").strip()
        supplier = inv.supplier.name if getattr(inv, "supplier", None) else (inv.supplier_name or "Unknown")
        key = (material, supplier)
        qty_yd3 = to_yd3(li.quantity, li.unit)
        cost = float(li.line_total or 0.0)
        if key not in agg:
            agg[key] = {"material": material, "supplier": supplier, "qty_yd3": 0.0, "cost": 0.0}
        agg[key]["qty_yd3"] += qty_yd3
        agg[key]["cost"] += cost

    data = sorted(agg.values(), key=lambda x: (x["material"], x["supplier"]))

    if fmt == "csv":
        import csv
        from io import StringIO
        sio = StringIO()
        w = csv.writer(sio)
        w.writerow(["material", "supplier", "qty_yd3", "cost"])
        for r in data:
            w.writerow([r["material"], r["supplier"], f"{r['qty_yd3']:.3f}", f"{r['cost']:.2f}"])
        resp = app.response_class(sio.getvalue(), mimetype="text/csv")
        resp.headers["Content-Disposition"] = "attachment; filename=purchases_report.csv"
        return resp

    return render_template("staff/reports_purchases.html", rows=data, from_date=from_date, to_date=to_date)


# --------------------------
# Expenses: UI & APIs
# --------------------------

@app.get("/staff/expenses")
@staff_required
def staff_expenses_list():
    from_date = (request.args.get("from") or "").strip() or None
    to_date = (request.args.get("to") or "").strip() or None
    category = (request.args.get("category") or "").strip() or None
    fmt = (request.args.get("format") or "").strip().lower()

    q = Expense.query
    if from_date:
        try:
            q = q.filter(Expense.date_dt >= datetime.strptime(from_date, "%Y-%m-%d").date())
        except Exception:
            pass
    if to_date:
        try:
            q = q.filter(Expense.date_dt <= datetime.strptime(to_date, "%Y-%m-%d").date())
        except Exception:
            pass
    if category:
        q = q.filter(Expense.category == category)

    rows = q.order_by(Expense.date_dt.desc().nullslast(), Expense.created_at.desc()).all()

    if fmt == "csv":
        import csv
        from io import StringIO
        sio = StringIO()
        w = csv.writer(sio)
        w.writerow(["date", "category", "description", "amount"])
        for e in rows:
            w.writerow([e.date or (e.date_dt.isoformat() if e.date_dt else ""), e.category, e.description or "", f"{(e.amount or 0):.2f}"])
        resp = app.response_class(sio.getvalue(), mimetype="text/csv")
        resp.headers["Content-Disposition"] = "attachment; filename=expenses.csv"
        return resp

    return render_template("staff/expenses_list.html", rows=rows, from_date=from_date, to_date=to_date, category=category)


@app.get("/staff/expenses/new")
@staff_required
def staff_expense_new():
    return render_template("staff/expense_edit.html")


@app.post("/api/staff/expenses")
@staff_required
def api_staff_expenses_save():
    try:
        body = request.get_json(force=True) or {}
    except Exception as ex:
        return jsonify({"ok": False, "error": f"Invalid JSON: {ex}"}), 400

    eid = body.get("id")
    date_s = (body.get("date") or "").strip() or None
    category = (body.get("category") or "").strip() or None
    description = (body.get("description") or "").strip() or None
    try:
        amount = float(body.get("amount") or 0)
    except Exception:
        amount = 0.0
    if not category or amount <= 0:
        return jsonify({"ok": False, "error": "category and positive amount are required"}), 400

    date_dt = None
    if date_s:
        try:
            date_dt = datetime.strptime(date_s, "%Y-%m-%d").date()
        except Exception:
            date_dt = None

    if eid:
        e = Expense.query.get(int(eid))
        if not e:
            return jsonify({"ok": False, "error": "Expense not found"}), 404
        e.date = date_s
        e.date_dt = date_dt
        e.category = category
        e.description = description
        e.amount = amount
    else:
        e = Expense(
            date=date_s, date_dt=date_dt, category=category, description=description,
            amount=amount, created_by=current_user.id if current_user.is_authenticated else None
        )
        db.session.add(e)

    db.session.commit()
    return jsonify({"ok": True})


@app.post("/api/staff/expenses/extract")
@staff_required
def api_staff_expenses_extract():
    try:
        body = request.get_json(force=True) or {}
    except Exception as ex:
        return jsonify({"ok": False, "error": f"Invalid JSON: {ex}"}), 400
    file_ids = body.get("file_ids") or []
    if not isinstance(file_ids, list) or not file_ids:
        return jsonify({"ok": False, "error": "file_ids must be a non-empty list"}), 400
    if not OPENAI_API_KEY:
        return jsonify({"ok": False, "error": "OPENAI_API_KEY is not set"}), 500
    # Resolve paths
    paths = []
    for fid in file_ids:
        fname = secure_filename(str(fid))
        path = os.path.join(app.config["UPLOAD_FOLDER"], fname)
        if not os.path.isfile(path):
            return jsonify({"ok": False, "error": f"File not found: {fid}"}), 400
        paths.append(path)
    data = propose_expenses_from_vision(paths)
    if not data:
        return jsonify({"ok": False, "error": "AI extraction failed"}), 502
    return jsonify({"ok": True, "data": data})


@app.post("/api/staff/expenses/ai-parse-text")
@staff_required
def api_staff_expenses_parse_text():
    try:
        body = request.get_json(force=True) or {}
    except Exception as ex:
        return jsonify({"ok": False, "error": f"Invalid JSON: {ex}"}), 400
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "text is required"}), 400
    if not OPENAI_API_KEY:
        return jsonify({"ok": False, "error": "OPENAI_API_KEY is not set"}), 500
    data = propose_expenses_from_text(text)
    if not data:
        return jsonify({"ok": False, "error": "AI parse failed"}), 502
    return jsonify({"ok": True, "data": data})


@app.get("/staff/reports/sales")
@staff_required
def staff_reports_sales():
    from_date = (request.args.get("from") or "").strip() or None
    to_date = (request.args.get("to") or "").strip() or None
    fmt = (request.args.get("format") or "").strip().lower()

    q = db.session.query(SalesReceipt, SalesReceiptLine).join(SalesReceiptLine, SalesReceiptLine.receipt_id == SalesReceipt.id)
    if from_date:
        q = q.filter(SalesReceipt.created_at >= datetime.strptime(from_date, "%Y-%m-%d"))
    if to_date:
        # include end-of-day
        q = q.filter(SalesReceipt.created_at < datetime.strptime(to_date, "%Y-%m-%d") + timedelta(days=1))

    rows = q.all()
    agg = {}
    for r, li in rows:
        product = normalize_material_name(li.material_key or li.item_name)
        if product == "other":
            continue
        qty = float(li.quantity or 0.0)
        # Convert to yd3 if unit is bag
        qty_yd3 = to_yd3(qty, li.unit or 'yd3', product)
        if (li.unit or 'yd3').lower() in ('bag','bags'):
            qty = qty_yd3
        revenue = float(li.line_total or (qty * float(li.unit_price or 0.0)))
        if product not in agg:
            agg[product] = {"product": product, "qty_yd3": 0.0, "revenue": 0.0}
        agg[product]["qty_yd3"] += qty
        agg[product]["revenue"] += revenue

    data = []
    for p, d in sorted(agg.items()):
        avg_price = (d["revenue"] / d["qty_yd3"]) if d["qty_yd3"] > 0 else 0.0
        data.append({"product": p, "qty_yd3": d["qty_yd3"], "revenue": d["revenue"], "avg_price": avg_price})

    if fmt == "csv":
        import csv
        from io import StringIO
        sio = StringIO()
        w = csv.writer(sio)
        w.writerow(["product", "qty_yd3", "revenue", "avg_price"])
        for r in data:
            w.writerow([r["product"], f"{r['qty_yd3']:.3f}", f"{r['revenue']:.2f}", f"{r['avg_price']:.2f}"])
        resp = app.response_class(sio.getvalue(), mimetype="text/csv")
        resp.headers["Content-Disposition"] = "attachment; filename=sales_report.csv"
        return resp

    return render_template("staff/reports_sales.html", rows=data, from_date=from_date, to_date=to_date)


@app.get("/staff/reports/gp")
@staff_required
def staff_reports_gp():
    from_date = (request.args.get("from") or "").strip() or None
    to_date = (request.args.get("to") or "").strip() or None
    fmt = (request.args.get("format") or "").strip().lower()

    # Sales aggregation
    q = db.session.query(SalesReceipt, SalesReceiptLine).join(SalesReceiptLine, SalesReceiptLine.receipt_id == SalesReceipt.id)
    if from_date:
        q = q.filter(SalesReceipt.created_at >= datetime.strptime(from_date, "%Y-%m-%d"))
    if to_date:
        q = q.filter(SalesReceipt.created_at < datetime.strptime(to_date, "%Y-%m-%d") + timedelta(days=1))
    rows = q.all()
    sales = {}
    for r, li in rows:
        product = normalize_material_name(li.material_key or li.item_name)
        if product == "other":
            continue
        qty_raw = float(li.quantity or 0.0)
        unit = (li.unit or 'yd3').lower()
        if product not in sales:
            sales[product] = {"qty_yd3_total": 0.0, "qty_yd3_bag": 0.0, "revenue": 0.0}
        if unit in ('bag','bags'):
            bag_yd3 = to_yd3(qty_raw, unit, product)
            yd3_total = bag_yd3
            sales[product]["qty_yd3_bag"] += bag_yd3
        else:
            bag_yd3 = 0.0
            yd3_total = to_yd3(qty_raw, unit, product)
            if unit == 'yd3':
                yd3_total = qty_raw
        price = float(li.unit_price or 0.0)
        revenue = float(li.line_total or (qty_raw * price))
        sales[product]["qty_yd3_total"] += yd3_total
        sales[product]["revenue"] += revenue

    # Weighted average cost from purchases
    avg_cost = _compute_weighted_avg_cost(from_date, to_date)

    # Compute GP per product (include bag material cost component)
    data = []
    grand_qty = 0.0
    grand_revenue = 0.0
    grand_cogs = 0.0
    for product in sorted(sales.keys()):
        qty = sales[product]["qty_yd3_total"]
        revenue = sales[product]["revenue"]
        cost_per_yd3 = float(avg_cost.get(product, 0.0))
        # Add packaging cost for bag-sold quantities only
        packaging_cost_per_yd3 = BAG_COST_PER_BAG * BAGS_PER_YD3 if product in ("sand","gravel","sharp_sand") else 0.0
        bag_qty_yd3 = sales[product].get("qty_yd3_bag", 0.0)
        cogs = (cost_per_yd3 * qty) + (packaging_cost_per_yd3 * bag_qty_yd3)
        gp = revenue - cogs
        margin = (gp / revenue * 100.0) if revenue > 0 else 0.0
        data.append({
            "product": product,
            "qty_yd3": qty,
            "revenue": revenue,
            "avg_cost_yd3": cost_per_yd3,
            "cogs": cogs,
            "gp": gp,
            "margin": margin,
        })
        grand_qty += qty
        grand_revenue += revenue
        grand_cogs += cogs

    grand = {
        "qty_yd3": grand_qty,
        "revenue": grand_revenue,
        "cogs": grand_cogs,
        "gp": (grand_revenue - grand_cogs),
        "margin": (((grand_revenue - grand_cogs) / grand_revenue) * 100.0) if grand_revenue > 0 else 0.0,
    }

    # By customer aggregation using avg_cost per product
    by_customer_map = {}
    for r, li in rows:
        product = normalize_material_name(li.material_key or li.item_name)
        if product == "other":
            continue
        qty_raw = float(li.quantity or 0.0)
        unit = (li.unit or 'yd3').lower()
        if unit in ('bag','bags'):
            bag_qty_yd3 = to_yd3(qty_raw, unit, product)
            yd3_total = bag_qty_yd3
        else:
            bag_qty_yd3 = 0.0
            yd3_total = to_yd3(qty_raw, unit, product)
            if unit == 'yd3':
                yd3_total = qty_raw
        revenue = float(li.line_total or (qty_raw * float(li.unit_price or 0.0)))
        cost_per_yd3 = float(avg_cost.get(product, 0.0))
        packaging_cost_per_yd3 = BAG_COST_PER_BAG * BAGS_PER_YD3 if product in ("sand","gravel","sharp_sand") else 0.0
        cogs = (cost_per_yd3 * yd3_total) + (packaging_cost_per_yd3 * bag_qty_yd3)
        customer = (r.customer_name or "Unknown").strip() or "Unknown"
        if customer not in by_customer_map:
            by_customer_map[customer] = {"customer": customer, "qty_yd3": 0.0, "revenue": 0.0, "cogs": 0.0}
        by_customer_map[customer]["qty_yd3"] += yd3_total
        by_customer_map[customer]["revenue"] += revenue
        by_customer_map[customer]["cogs"] += cogs

    by_customer = []
    for cust, d in sorted(by_customer_map.items()):
        gp = d["revenue"] - d["cogs"]
        margin = (gp / d["revenue"] * 100.0) if d["revenue"] > 0 else 0.0
        by_customer.append({
            "customer": cust,
            "qty_yd3": d["qty_yd3"],
            "revenue": d["revenue"],
            "cogs": d["cogs"],
            "gp": gp,
            "margin": margin,
        })

    # Operating expenses (within date range)
    opex_q = Expense.query
    if from_date:
        try:
            opex_q = opex_q.filter(Expense.date_dt >= datetime.strptime(from_date, "%Y-%m-%d").date())
        except Exception:
            pass
    if to_date:
        try:
            opex_q = opex_q.filter(Expense.date_dt <= datetime.strptime(to_date, "%Y-%m-%d").date())
        except Exception:
            pass
    opex_total = sum(float(e.amount or 0.0) for e in opex_q.all())

    net_profit = (grand["gp"] - opex_total)

    if fmt == "csv":
        import csv
        from io import StringIO
        sio = StringIO()
        w = csv.writer(sio)
        w.writerow(["product", "qty_yd3", "revenue", "avg_cost_yd3", "cogs", "gp", "margin_pct"])
        for r in data:
            w.writerow([
                r["product"], f"{r['qty_yd3']:.3f}", f"{r['revenue']:.2f}", f"{r['avg_cost_yd3']:.2f}",
                f"{r['cogs']:.2f}", f"{r['gp']:.2f}", f"{r['margin']:.1f}"
            ])
        # Grand total row
        w.writerow([
            "TOTAL",
            f"{grand['qty_yd3']:.3f}", f"{grand['revenue']:.2f}", "",
            f"{grand['cogs']:.2f}", f"{grand['gp']:.2f}", f"{grand['margin']:.1f}"
        ])
        # Opex and Net Profit rows
        w.writerow(["OPEX", "", "", "", f"{opex_total:.2f}", "", ""]) 
        w.writerow(["NET_PROFIT", "", "", "", "", f"{net_profit:.2f}", ""]) 
        resp = app.response_class(sio.getvalue(), mimetype="text/csv")
        resp.headers["Content-Disposition"] = "attachment; filename=gp_report.csv"
        return resp

    return render_template("staff/reports_gp.html", rows=data, from_date=from_date, to_date=to_date, grand=grand, by_customer=by_customer, opex_total=opex_total, net_profit=net_profit)

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

# --------------------------
# Staff Purchases APIs
# --------------------------

@app.post("/api/staff/purchases/extract")
@staff_required
def api_staff_extract_invoice():
    """Body: { file_ids: [string] } -> AI parsed invoice details"""
    try:
        body = request.get_json(force=True) or {}
    except Exception as ex:
        return jsonify({"ok": False, "error": f"Invalid JSON: {ex}"}), 400
    file_ids = body.get("file_ids") or []
    if not isinstance(file_ids, list) or not file_ids:
        return jsonify({"ok": False, "error": "file_ids must be a non-empty list"}), 400
    # Validate and map to paths under uploads
    paths = []
    for fid in file_ids:
        fname = secure_filename(str(fid))
        path = os.path.join(app.config["UPLOAD_FOLDER"], fname)
        if not os.path.isfile(path):
            return jsonify({"ok": False, "error": f"File not found: {fid}"}), 400
        paths.append(path)

    if not OPENAI_API_KEY:
        return jsonify({"ok": False, "error": "OPENAI_API_KEY is not set"}), 500

    data = propose_invoice_from_vision(paths)
    if not data or not isinstance(data, dict):
        return jsonify({"ok": False, "error": "AI extraction failed"}), 502
    return jsonify({"ok": True, "data": data})


@app.post("/api/staff/purchases/ai-parse-text")
@staff_required
def api_staff_parse_text():
    try:
        body = request.get_json(force=True) or {}
    except Exception as ex:
        return jsonify({"ok": False, "error": f"Invalid JSON: {ex}"}), 400
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "text is required"}), 400
    if not OPENAI_API_KEY:
        return jsonify({"ok": False, "error": "OPENAI_API_KEY is not set"}), 500
    data = propose_purchase_from_text(text)
    if not data or not isinstance(data, dict):
        return jsonify({"ok": False, "error": "AI parse failed"}), 502
    return jsonify({"ok": True, "data": data})


@app.post("/api/staff/purchases")
@staff_required
def api_staff_save_purchase():
    """Create or update a purchase invoice with line items.
    Body may include id (to update), fields of PurchaseInvoice and lines[] (with optional id to update).
    """
    try:
        body = request.get_json(force=True) or {}
    except Exception as ex:
        return jsonify({"ok": False, "error": f"Invalid JSON: {ex}"}), 400

    supplier_name = (body.get("supplier_name") or "").strip() or None
    supplier_id = body.get("supplier_id")
    invoice_date = (body.get("invoice_date") or "").strip() or None
    invoice_number = (body.get("invoice_number") or "").strip() or None
    currency = (body.get("currency") or "TTD").strip() or "TTD"
    uploaded_files = body.get("uploaded_files") or []
    status = (body.get("status") or "draft").strip() or "draft"
    lines = body.get("lines") or []
    invoice_id = body.get("id")

    if not isinstance(lines, list) or not lines:
        return jsonify({"ok": False, "error": "lines must be a non-empty list"}), 400

    # Create supplier if only supplier_name provided and no supplier_id
    sup = None
    if supplier_id:
        sup = Supplier.query.get(int(supplier_id))
    elif supplier_name:
        sup = Supplier(name=supplier_name)
        db.session.add(sup)
        db.session.flush()

    # Parse invoice_date as date
    inv_date_dt = None
    if invoice_date:
        try:
            inv_date_dt = datetime.strptime(invoice_date, "%Y-%m-%d").date()
        except Exception:
            inv_date_dt = None

    if invoice_id:
        inv = PurchaseInvoice.query.get(int(invoice_id))
        if not inv:
            return jsonify({"ok": False, "error": "Invoice not found"}), 404
        inv.supplier_id = sup.id if sup else None
        inv.supplier_name = None if sup else supplier_name
        inv.invoice_date = invoice_date
        inv.invoice_date_dt = inv_date_dt
        inv.invoice_number = invoice_number
        inv.currency = currency
        inv.status = status
        inv.uploaded_files = json.dumps(uploaded_files) if isinstance(uploaded_files, list) else (uploaded_files or None)
    else:
        inv = PurchaseInvoice(
            supplier_id=sup.id if sup else None,
            supplier_name=None if sup else supplier_name,
            invoice_date=invoice_date,
            invoice_date_dt=inv_date_dt,
            invoice_number=invoice_number,
            currency=currency,
            status=status,
            uploaded_files=json.dumps(uploaded_files) if isinstance(uploaded_files, list) else (uploaded_files or None),
            created_by=current_user.id if current_user.is_authenticated else None,
        )
        db.session.add(inv)
        db.session.flush()

    # Upsert line items
    subtotal = 0.0
    existing_ids = set()
    for li in lines:
        try:
            li_id = li.get("id")
            desc = (li.get("description") or "").strip()
            unit = (li.get("unit") or "").strip()
            qty = float(li.get("qty") or 0)
            unit_price = li.get("unit_price")
            unit_price = float(unit_price) if unit_price not in (None, "",) else None
            line_total = li.get("line_total")
            line_total = float(line_total) if line_total not in (None, "",) else None
        except Exception:
            continue
        if not desc or not unit or qty <= 0:
            continue
        if line_total is None and unit_price is not None:
            line_total = unit_price * qty
        if line_total is not None:
            subtotal += float(line_total)

        if li_id:
            row = PurchaseLineItem.query.get(int(li_id))
            if row and row.invoice_id == inv.id:
                row.description = desc
                row.category = (li.get("category") or None)
                row.material_key = (li.get("material_key") or None)
                row.unit = unit
                row.quantity = qty
                row.unit_price = unit_price
                row.line_total = line_total
                existing_ids.add(row.id)
                continue
        # create new
        row = PurchaseLineItem(
            invoice_id=inv.id,
            description=desc,
            category=(li.get("category") or None),
            material_key=(li.get("material_key") or None),
            unit=unit,
            quantity=qty,
            unit_price=unit_price,
            line_total=line_total,
        )
        db.session.add(row)
        db.session.flush()
        existing_ids.add(row.id)

    # Delete removed lines when updating
    if invoice_id:
        to_delete = PurchaseLineItem.query.filter(
            PurchaseLineItem.invoice_id == inv.id,
            ~PurchaseLineItem.id.in_(existing_ids)
        ).all()
        for d in to_delete:
            db.session.delete(d)

    tax = body.get("tax")
    total = body.get("total")
    try:
        tax_f = float(tax) if tax not in (None, "",) else None
    except (TypeError, ValueError):
        tax_f = None
    try:
        total_f = float(total) if total not in (None, "",) else None
    except (TypeError, ValueError):
        total_f = None
    if total_f is None:
        total_f = subtotal + (tax_f or 0.0)

    inv.subtotal = round(subtotal, 2)
    inv.tax = round(tax_f or 0.0, 2)
    inv.total = round(total_f, 2)

    db.session.commit()
    return jsonify({"ok": True, "id": inv.id})

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
        "staff": bool(getattr(current_user, "is_staff", False)) if current_user.is_authenticated else False,
        "endpoints": {
            "purchases_extract": "/api/staff/purchases/extract",
            "purchases_text": "/api/staff/purchases/ai-parse-text",
            "purchases_save": "/api/staff/purchases",
            "receipts_create": "/api/staff/receipts",
            "receipt_print": "/staff/receipts/<id>/print",
        }
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
    # Note: Template filename has a capital 'A' on disk; Linux/Docker is case-sensitive
    return render_template("buildAdvisor.html")

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
# Staff Sales (Billing) APIs
# --------------------------

@app.post("/api/staff/receipts")
@staff_required
def api_staff_create_receipt():
    try:
        body = request.get_json(force=True) or {}
    except Exception as ex:
        return jsonify({"ok": False, "error": f"Invalid JSON: {ex}"}), 400

    customer_name = (body.get("customer_name") or "").strip() or None
    lines = body.get("lines") or []
    notes = (body.get("notes") or "").strip() or None
    if not isinstance(lines, list) or not lines:
        return jsonify({"ok": False, "error": "lines must be a non-empty list"}), 400

    receipt = SalesReceipt(
        customer_name=customer_name,
        notes=notes,
        created_by=current_user.id if current_user.is_authenticated else None,
    )
    db.session.add(receipt)
    db.session.flush()

    subtotal = 0.0
    for li in lines:
        try:
            name = (li.get("item_name") or li.get("name") or "").strip()
            unit = (li.get("unit") or "yd3").strip()
            qty = float(li.get("quantity") or li.get("qty") or 0)
            price = float(li.get("unit_price") or li.get("price") or 0)
        except Exception:
            continue
        if not name or qty <= 0 or price < 0:
            continue
        line_total = qty * price
        subtotal += line_total
        db.session.add(SalesReceiptLine(
            receipt_id=receipt.id,
            item_name=name,
            unit=unit,
            quantity=qty,
            unit_price=price,
            line_total=line_total,
            material_key=(li.get("material_key") or None),
        ))

    receipt.subtotal = round(subtotal, 2)
    receipt.tax = 0.0
    receipt.total = receipt.subtotal
    # Simple receipt number
    receipt.receipt_no = f"R{receipt.id:06d}"
    db.session.commit()
    return jsonify({"ok": True, "id": receipt.id, "receipt_no": receipt.receipt_no})


@app.get("/staff/receipts/<int:rid>/print")
@staff_required
def staff_print_receipt(rid: int):
    r = SalesReceipt.query.get_or_404(rid)
    lines = SalesReceiptLine.query.filter_by(receipt_id=r.id).all()
    return render_template("print_receipt.html", receipt=r, lines=lines)

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
        db.create_all()  # creates any missing tables
        # Best-effort: add missing 'is_staff' column for existing User (SQLite only)
        try:
            # SQLite pragma to introspect table; add column if missing
            from sqlalchemy import text
            cols = db.session.execute(text("PRAGMA table_info(user);")).fetchall()
            col_names = {c[1] for c in cols} if cols else set()
            if "is_staff" not in col_names and app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
                db.session.execute(text("ALTER TABLE user ADD COLUMN is_staff BOOLEAN NOT NULL DEFAULT 0;"))
                db.session.commit()
        except Exception:
            # ignore if db engine doesn't support or already exists
            pass
    # Bind to 0.0.0.0 and respect PORT for hosting platforms (e.g., Railway)
    port = int(os.getenv("PORT", "5000"))
    host = os.getenv("HOST", "0.0.0.0")
    debug = str(os.getenv("FLASK_DEBUG", "1")).lower() in ("1", "true", "yes")
    app.run(host=host, port=port, debug=debug)
