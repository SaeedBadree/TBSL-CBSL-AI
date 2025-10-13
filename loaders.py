# loaders.py
import csv, re, os
from pathlib import Path

FT_TO_M = 0.3048
DEFAULT_19FT_M = 19 * FT_TO_M
# kg per meter approximations for common rebar sizes
KG_PER_M = {
    '3/8': 0.560,
    '1/2': 0.994,
    '5/8': 1.550,
    '3/4': 2.260,  # optional if you enable 3/4" keys
}

# ---------- helpers ----------

def to_float(x):
    """Parse prices like '1,090.00', '$350', 'TTD 95' or simple formulas '390*1.308'."""
    s = str(x)
    s = s.replace(",", "").replace("$", "").replace("TTD", "").strip()
    try:
        if any(ch in s for ch in "/*+-") and re.match(r"^[\d\.\s/*+-]+$", s):
            return float(eval(s))
    except Exception:
        pass
    m = re.findall(r"[\d\.]+", s)
    return float(m[-1]) if m else None

_key_norm_re = re.compile(r"[^a-z0-9]+")

def norm_key(k: str) -> str:
    """lowercase and strip non-alphanum (so 'Item Name' -> 'itemname', 'Selling' -> 'selling')."""
    return _key_norm_re.sub("", (k or "").lower())

def read_csv_rows(path):
    """
    Read rows AND return each row with normalized keys added.
    For example {'Item Name': 'x', 'Selling': '10'} becomes accessible via
    row['itemname'] and row['selling'].
    """
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    with open(p, "r", encoding="utf-8", errors="ignore") as f:
        rdr = csv.DictReader(f)
        orig_fields = list(rdr.fieldnames or [])
        norm_fields = [norm_key(c) for c in orig_fields]
        for raw in rdr:
            # build normalized dict
            row = {}
            for orig, norm in zip(orig_fields, norm_fields):
                row[orig] = (raw.get(orig) or "").strip()
                row[norm] = row[orig]  # duplicate under normalized key
            rows.append(row)
    return rows

def parse_length(text):
    """
    Detect lengths like '20ft', '6 m', or implicit 'x20' (feet) as in 'Z Purlin 2x4x20 1.2'.
    Returns (value, unit) where unit is 'ft' or 'm'.
    """
    up = text.upper().replace("×", "X")
    # explicit meters
    m = re.search(r"(\d+(\.\d+)?)\s*(M|METERS|METRES)\b", up)
    if m: return float(m.group(1)), "m"
    # explicit feet
    m = re.search(r"(\d+(\.\d+)?)\s*(FT|FEET|FOOT)\b", up)
    if m: return float(m.group(1)), "ft"
    # implicit last 'xNN' => feet (e.g., 2x4x20 -> 20 ft)
    m = re.search(r"x\s*(\d+(\.\d+)?)\b(?!\s*(MM|CM|M))", up)
    if m:
        val = float(m.group(1))
        if 5 <= val <= 40:
            return val, "ft"
    return None, None

def steel_size_from_text(text):
    """Identify rebar diameter from text."""
    up = text.upper().replace(" ", "")
    pats = {
        "3/8": r"\b3/8\b|10MM",
        "1/2": r"\b1/2\b|12MM",
        "5/8": r"\b5/8\b|16MM",
        "3/4": r"\b3/4\b|20MM",  # optional
    }
    for size, pat in pats.items():
        if re.search(pat, up):
            return size
    return None

def steel_grade_from_text(text):
    up = text.upper()
    if re.search(r"CORR|DEFORM|RIB|TENS", up): return "corrugated"
    if re.search(r"MILD|SMOOTH|\bMS\b", up):   return "mild"
    return "corrugated"

def per_meter(price, length_value, length_unit, size_in, name_up):
    """Convert listed price (per piece/ft/m/kg) to per-meter pricing."""
    if price is None: return None
    if length_value and length_unit:
        if str(length_unit).lower().startswith("m"):  return price / float(length_value)
        if str(length_unit).lower().startswith("f"):  return price / (float(length_value) * FT_TO_M)
    # per-kg line?
    if " KG" in name_up or "PER KG" in name_up:
        kgpm = KG_PER_M.get(size_in)
        if kgpm: return price * kgpm
    # default: assume 19 ft stick price
    return price / DEFAULT_19FT_M

# ---------- loaders ----------

def load_aggregates(path):
    """CSV with: key,price (per m^3)."""
    out = {}
    for r in read_csv_rows(path):
        k = (r.get("key") or r.get("Key") or r.get("KEY") or r.get("key") or "").strip()
        v = to_float(r.get("price") or r.get("Price") or r.get("PRICE") or r.get("price") or r.get("selling"))
        if k and v is not None:
            out[k] = v
    return out

def load_steel(path):
    """
    Flexible steel loader for your CSV.
    Accepts headers like 'name,price' OR 'Item Name,Selling' (case/space insensitive).
    - Rebar (corr/mild) -> rebar_corr_1_2_m etc. (per-meter)
    - Purlins in this file -> purlin_z_m / purlin_c_m (per-meter)
    - Length inferred from '19ft', '6m', or implicit 'x20' (20 ft). If none for rebar, defaults to 19 ft.
    """
    prices = {}
    rows = []
    for r in read_csv_rows(path):
        # pull using normalized keys as well
        name = r.get("name") or r.get("Name") or r.get("ITEM") or r.get("Item Name") or r.get("itemname") or ""
        price = to_float(
            r.get("price") or r.get("Price") or r.get("SELLING") or r.get("Selling") or r.get("selling")
        )
        if not name or price is None:
            continue
        up = name.upper()

        # --- PURLINS (Z/C) ---
        if "PURLIN" in up:
            lv = r.get("length_value"); lu = r.get("length_unit")
            try:
                lv = float(lv) if lv not in (None, "") else None
            except Exception:
                lv = None
            if not lv or not lu:
                lv2, lu2 = parse_length(up)
                lv, lu = lv or lv2, lu or lu2
            per_m = price
            if lv and lu:
                if lu.lower().startswith("m"): per_m = price / lv
                elif lu.lower().startswith("f"): per_m = price / (lv * FT_TO_M)
            kind = "purlin_z_m" if (" Z " in f" {up} " or up.startswith("Z ")) else (
                   "purlin_c_m" if (" C " in f" {up} " or up.startswith("C ")) else None)
            if kind:
                prices[kind] = min(prices.get(kind, 1e18), per_m)
            rows.append({
                "name": name, "kind": "purlin",
                "length_value": lv or "", "length_unit": lu or "",
                "unit_price": price, "price_per_m": per_m
            })
            continue

        # --- REBAR (corrugated/mild) ---
        size_in = r.get("size_in") or steel_size_from_text(up)
        grade = (r.get("grade") or steel_grade_from_text(up)).lower()

        lv = r.get("length_value"); lu = r.get("length_unit")
        try:
            lv = float(lv) if lv not in (None, "") else None
        except Exception:
            lv = None
        if not lv or not lu:
            lv2, lu2 = parse_length(up)
            lv, lu = lv or lv2, lu or lu2
        # default for rebar
        if not lv or not lu:
            lv, lu = 19.0, "ft"

        per_m = per_meter(price, lv, lu, size_in, up)
        if size_in:
            key = f"rebar_{'corr' if grade.startswith('corr') else 'mild'}_{size_in.replace('/','_')}_m"
            prices[key] = min(prices.get(key, 1e18), per_m)
        rows.append({
            "name": name, "kind": "rebar",
            "size_in": size_in or "", "grade": grade,
            "length_value": lv, "length_unit": lu,
            "unit_price": price, "price_per_m": per_m
        })

    return prices, rows

def load_building(path):
    """
    General building materials mapped to app keys:
    - Cement (Eco/Premium/Loose lb) -> cement_bag(_eco/_premium) / cement_loose_lb (converted to bag if CEMENT_GRADE=loose)
    - Blocks (4/6/8 in, optional clay) -> block_4in/block_6in/block_8in/block_clay_4in
    - Mesh A142 -> mesh_A142_sheet
    - Tie wire -> tie_wire_kg
    - Purlins (Z/C) -> purlin_z_m / purlin_c_m (per-meter)
    - Paint (… GAL) -> paint_gal
    """
    prices = {}
    purlins, blocks, cement = [], [], []
    for r in read_csv_rows(path):
        name = r.get("name") or r.get("Name") or r.get("ITEM") or r.get("Item") or r.get("Item Name") or r.get("itemname") or ""
        price = to_float(r.get("price") or r.get("Price") or r.get("SELLING") or r.get("Selling") or r.get("selling"))
        if not name or price is None: 
            continue
        up = name.upper()

        # cement
        if "CEMENT" in up and not any(w in up for w in ["BOARD","ADHESIVE","THINSET","CONTACT"]):
            if "PREMIUM" in up: prices["cement_bag_premium"] = min(prices.get("cement_bag_premium", 1e18), price)
            elif "ECO" in up:  prices["cement_bag_eco"]     = min(prices.get("cement_bag_eco", 1e18), price)
            elif "LOOSE" in up or " PER LB" in up or "LB" in up:
                prices["cement_loose_lb"] = min(prices.get("cement_loose_lb", 1e18), price)
            else:
                prices["cement_bag"] = min(prices.get("cement_bag", 1e18), price)
            cement.append({"name": name, "price": price})
            continue

        # blocks
        if "BLOCK" in up:
            size=None
            if re.search(r'(^|\s)4\s*"?\b', up) or "4X8X16" in up: size="4"
            if re.search(r'(^|\s)6\s*"?\b', up) or "6X8X16" in up: size="6"
            if re.search(r'(^|\s)8\s*"?\b', up) or "8X8X16" in up: size="8"
            clay = ("CLAY" in up) or ("RED" in up)
            if size:
                key = "block_clay_4in" if (clay and size=="4") else (f"block_{size}in" if not clay else None)
                if key:
                    prices[key] = min(prices.get(key, 1e18), price)
                    blocks.append({"name":name,"size_in":size,"type":"clay" if clay else "concrete","unit":"piece","price":price})
            continue

        # mesh
        if "MESH" in up and "A142" in up:
            prices["mesh_A142_sheet"] = min(prices.get("mesh_A142_sheet", 1e18), price)
            continue

        # tie wire
        if "TIE WIRE" in up or "BINDING WIRE" in up:
            prices["tie_wire_kg"] = min(prices.get("tie_wire_kg", 1e18), price)
            continue

        # purlins
        if "PURLIN" in up:
            lv, lu = parse_length(up)
            per_m = price
            if lv and lu:
                if lu.lower().startswith("m"): per_m = price / lv
                elif lu.lower().startswith("f"): per_m = price / (lv * FT_TO_M)
            kind = "purlin_z_m" if (" Z " in f" {up} " or up.startswith("Z ")) else (
                   "purlin_c_m" if (" C " in f" {up} " or up.startswith("C ")) else None)
            if kind:
                prices[kind] = min(prices.get(kind, 1e18), per_m)
                purlins.append({"kind":kind, "name":name, "price_per_m": per_m})
            continue

        # paint (optional)
        if ("PAINT" in up or "EMULSION" in up) and "GAL" in up:
            prices["paint_gal"] = min(prices.get("paint_gal", 1e18), price)
            continue

    # cement default preference
    grade = (os.getenv("CEMENT_GRADE") or "").strip().lower()
    if grade == "eco" and "cement_bag_eco" in prices:
        prices["cement_bag"] = prices["cement_bag_eco"]
    elif grade == "premium" and "cement_bag_premium" in prices:
        prices["cement_bag"] = prices["cement_bag_premium"]
    elif grade == "loose" and "cement_loose_lb" in prices:
        bag_lbs = 42.5 * 2.20462
        prices["cement_bag"] = round(bag_lbs * float(prices["cement_loose_lb"]), 2)

    return prices, {"purlins": purlins, "blocks": blocks, "cement": cement}

def merge_prices(*dicts):
    """Merge price dicts, keeping the lowest numeric price for duplicate keys."""
    out = {}
    for d in dicts:
        for k, v in (d or {}).items():
            if v is None: continue
            out[k] = min(out.get(k, float(v)), float(v)) if k in out else float(v)
    return out
