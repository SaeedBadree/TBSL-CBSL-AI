# ai_text.py
import os
import json
import base64
import logging
from typing import Dict, Any, List, Optional

import httpx
from openai import OpenAI

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

# Model can be overridden via env
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Inventory keys you price
ALLOWED_KEYS: List[str] = [
    # Aggregates
    "sand_m3", "sharp_sand_m3", "gravel_m3", "red_sand_m3", "backfill_m3", "soakaway_boulders_m3",
    # Cement
    "cement_bag", "cement_bag_eco", "cement_bag_premium", "cement_loose_lb",
    # Blocks
    "block_4in", "block_6in", "block_8in", "block_clay_4in",
    # Steel (per meter)
    "rebar_corr_3_8_m", "rebar_corr_1_2_m", "rebar_corr_5_8_m",
    "rebar_mild_3_8_m", "rebar_mild_1_2_m", "rebar_mild_5_8_m",
    # Mesh / wire / purlins
    "mesh_A142_sheet", "tie_wire_kg", "purlin_z_m", "purlin_c_m",
    # Paint
    "paint_gal",
]

# Units we accept and will normalize to
_ALLOWED_UNITS = {"m3", "m", "kg", "bag", "sheet", "pcs", "gal", "lb"}

# Staff purchases/receipts also allow cubic yards
_ALLOWED_UNITS_STAFF = {"yd3", "m3", "m", "kg", "bag", "sheet", "pcs", "gal", "lb"}

def _make_client() -> Optional[OpenAI]:
    """Create an OpenAI client. Returns None if no API key is configured."""
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        log.warning("OPENAI_API_KEY is not set")
        return None

    proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
    if proxy:
        http_client = httpx.Client(proxies=proxy, timeout=60.0)
    else:
        http_client = httpx.Client(timeout=60.0)

    return OpenAI(api_key=key, http_client=http_client)


def _get_model_sequence(kind: str) -> List[str]:
    """Return primary model followed by fallback candidates for the given kind.
    kind: "text" | "vision"
    Can be overridden by env OPENAI_MODEL and OPENAI_MODEL_FALLBACKS (comma-separated).
    """
    primary = os.getenv("OPENAI_MODEL", MODEL)
    fallbacks_env = os.getenv("OPENAI_MODEL_FALLBACKS", "").strip()

    if fallbacks_env:
        candidates = [m.strip() for m in fallbacks_env.split(",") if m.strip()]
    else:
        if kind == "vision":
            # Vision-capable default candidates
            candidates = ["gpt-4o-mini", "gpt-4o"]
        else:
            # General text candidates
            candidates = ["gpt-4o-mini", "gpt-4o"]

    # Ensure primary is first and de-duplicate while preserving order
    sequence: List[str] = []
    for m in [primary] + candidates:
        if m and m not in sequence:
            sequence.append(m)
    return sequence


def _chat_completion_with_fallback(
    client: OpenAI,
    *,
    messages: List[Dict[str, Any]],
    response_format: Optional[Dict[str, Any]] = None,
    timeout: float = 60.0,
    model_kind: str = "text",
):
    """Try primary model then fallbacks until one succeeds, else re-raise last error."""
    last_err: Optional[BaseException] = None
    for model_name in _get_model_sequence(model_kind):
        try:
            if response_format is not None:
                return client.chat.completions.create(
                    model=model_name,
                    response_format=response_format,
                    messages=messages,
                    timeout=timeout,
                )
            else:
                return client.chat.completions.create(
                    model=model_name,
                    messages=messages,
                    timeout=timeout,
                )
        except Exception as e:  # API errors: BadRequestError, RateLimitError, etc.
            log.warning("Model %s failed: %s", model_name, e)
            last_err = e
            continue
    if last_err is not None:
        raise last_err
    raise RuntimeError("No model candidates available for completion")

def _norm_unit(u: str) -> str:
    """Normalize unit strings to our canonical set."""
    if not isinstance(u, str):
        return ""
    u = u.strip().lower()
    # common synonyms
    if u in {"meter", "meters", "metre", "metres"}:
        return "m"
    if u in {"m^3", "m³", "cubic meter", "cubic meters", "cubic metre", "cubic metres"}:
        return "m3"
    if u in {"bags"}:
        return "bag"
    if u in {"sheets"}:
        return "sheet"
    if u in {"pieces", "piece"}:
        return "pcs"
    if u in {"gallon", "gallons"}:
        return "gal"
    if u in {"pound", "pounds"}:
        return "lb"
    return u

def _norm_unit_staff(u: str) -> str:
    """Normalize unit strings for staff purchases, including cubic yards."""
    if not isinstance(u, str):
        return ""
    u = (u or "").strip().lower()
    # yards synonyms
    if u in {"yd", "yds", "yard", "yards", "yd^3", "yd³", "cubic yard", "cubic yards"}:
        return "yd3"
    # fall back to generic normalization
    u2 = _norm_unit(u)
    return u2

def _validate_lines(raw: Any) -> List[Dict[str, Any]]:
    """Validate/clean AI-returned lines."""
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for it in raw:
        if not isinstance(it, dict):
            continue
        k = it.get("key")
        qty = it.get("qty")
        unit = _norm_unit(it.get("unit", ""))

        if k not in ALLOWED_KEYS:
            continue
        try:
            qty_f = float(qty)
        except (TypeError, ValueError):
            continue
        if qty_f <= 0:
            continue
        if unit not in _ALLOWED_UNITS:
            continue

        out.append({"key": k, "qty": qty_f, "unit": unit})
    return out

def propose_bom_with_ai(prompt: str, spec: dict) -> dict:
    """
    Ask the model for a STRICT JSON object:
    {
      "lines": [{"key": <ALLOWED_KEYS item>, "qty": <number>, "unit": "m3|m|kg|bag|sheet|pcs|gal|lb"}],
      "notes": "short rationale"
    }
    Returns {} on failure.
    """
    client = _make_client()
    if not client:
        return {}

    system = (
        "You are a building-materials estimator for Trinidad & Tobago.\n"
        "Return ONLY a JSON object with keys 'lines' and 'notes'.\n"
        "'lines' is a list of items, each with:\n"
        "  - key: must be one of the allowed inventory keys I provide.\n"
        "  - qty: a positive number.\n"
        "  - unit: one of m3, m, kg, bag, sheet, pcs, gal, lb (use these EXACT tokens).\n"
        "If the project is a slab/driveway/pad, include reinforcement: "
        "'mesh_A142_sheet' (typ. one layer) or a rebar grid using 'rebar_corr_3_8_m'.\n"
        "Use units that match the key (e.g. *_m3 uses m3; rebar_*_m uses m; cement_bag uses bag)."
    )
    user = (
        f"User request: {prompt}\n\n"
        f"Parsed spec (optional): {json.dumps(spec, ensure_ascii=False)}\n\n"
        f"Allowed keys ONLY: {ALLOWED_KEYS}\n\n"
        "Respond as pure JSON. Example shape:\n"
        "{\n"
        '  "lines": [\n'
        '    {"key":"sharp_sand_m3","qty":2.4,"unit":"m3"},\n'
        '    {"key":"gravel_m3","qty":4.8,"unit":"m3"},\n'
        '    {"key":"cement_bag","qty":18,"unit":"bag"},\n'
        '    {"key":"mesh_A142_sheet","qty":3,"unit":"sheet"}\n'
        '  ],\n'
        '  "notes":"Short rationale and assumptions."\n'
        "}"
    )

    try:
        resp = _chat_completion_with_fallback(
            client,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            timeout=60.0,
            model_kind="text",
        )
        content = (resp.choices[0].message.content or "").strip()
        data = json.loads(content)  # should already be a JSON object due to response_format

        cleaned = _validate_lines(data.get("lines"))
        return {"lines": cleaned, "notes": data.get("notes", "")}
    except Exception as e:
        log.exception("propose_bom_with_ai failed: %s", e)
        return {}

def expand_steps_with_ai(prompt: str, spec: dict, estimate: dict, default_text: str) -> str:
    """
    Optional narrative to accompany the estimate.
    Returns default_text if API call fails or key missing.
    """
    client = _make_client()
    if not client:
        return default_text

    sys_msg = (
        "You are a helpful building advisor in Trinidad & Tobago. "
        "Write a short, practical plan using clear bullet points. "
        "Use metric primarily, but acknowledge local steel sizes (3/8, 1/2, 5/8) and brands (e.g., TCL cement). "
        "Keep it concise and actionable for a homeowner."
    )
    user_msg = (
        f"Request: {prompt}\n\n"
        f"Parsed spec (optional): {json.dumps(spec, ensure_ascii=False)}\n\n"
        f"Estimate lines: {json.dumps(estimate.get('lines', []), ensure_ascii=False)}\n"
        f"Estimated total: {estimate.get('total', 0)}\n\n"
        "Give a brief step-by-step plan and a few tips. Avoid brand promotions; keep it neutral and practical."
    )

    try:
        resp = _chat_completion_with_fallback(
            client,
            messages=[
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg},
            ],
            timeout=60.0,
            model_kind="text",
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or default_text
    except Exception as e:
        log.exception("expand_steps_with_ai failed: %s", e)
        return default_text


# --------------------------
# Vision (images/PDF → BOM)
# --------------------------
def _file_to_data_url(image_path: str) -> Optional[str]:
    try:
        with open(image_path, "rb") as f:
            raw = f.read()
        # Guess mime by extension; keep simple for v1
        ext = os.path.splitext(image_path)[1].lower()
        mime = "image/jpeg" if ext in {".jpg", ".jpeg"} else (
            "image/png" if ext == ".png" else (
                "image/webp" if ext == ".webp" else (
                    "image/gif" if ext == ".gif" else "application/octet-stream"
                )
            )
        )
        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception:
        log.exception("_file_to_data_url failed for %s", image_path)
        return None


def _pdf_to_images(pdf_path: str, max_pages: int = 3, scale: float = 2.0) -> List[str]:
    """Render first N pages of a PDF to temporary PNG images; return file paths.
    Requires pypdfium2 and Pillow. Returns [] on failure.
    """
    try:
        import pypdfium2 as pdfium  # type: ignore
    except Exception:
        log.warning("pypdfium2 not installed; cannot rasterize PDFs")
        return []

    from tempfile import mkdtemp
    from pathlib import Path

    out_dir = Path(mkdtemp(prefix="pdfimgs_"))
    paths: List[str] = []
    try:
        pdf = pdfium.PdfDocument(pdf_path)
        count = min(int(pdf.page_count), int(max_pages))
        for i in range(count):
            page = pdf[i]
            bitmap = page.render(scale=scale)
            pil_image = bitmap.to_pil()  # requires Pillow
            out_path = out_dir / f"page_{i+1}.png"
            pil_image.save(str(out_path), format="PNG")
            paths.append(str(out_path))
        return paths
    except Exception:
        log.exception("PDF rasterization failed for %s", pdf_path)
        return []


def propose_bom_from_vision(file_paths: List[str], spec: dict) -> dict:
    """
    Build a strict JSON BOM from images/PDFs using a vision-capable model.
    Returns {"lines": [...], "notes": str} or {} on failure.
    """
    client = _make_client()
    if not client:
        return {}

    # Prepare content blocks
    content: List[Dict[str, Any]] = [
        {"type": "text", "text": (
            "Extract a building bill of materials (BOM) mapped to the provided allowed keys. "
            "Return ONLY JSON with keys 'lines' and 'notes'. Units must be one of: m3, m, kg, bag, sheet, pcs, gal, lb."
        )}
    ]

    # Expand PDFs into images
    expanded_images: List[str] = []
    for p in (file_paths or []):
        ext = os.path.splitext(p)[1].lower()
        if ext == ".pdf":
            expanded_images.extend(_pdf_to_images(p, max_pages=3))
        else:
            expanded_images.append(p)

    # Convert images to data URLs
    for img_path in expanded_images:
        data_url = _file_to_data_url(img_path)
        if not data_url:
            continue
        content.append({
            "type": "image_url",
            "image_url": {"url": data_url}
        })

    system = (
        "You are a building-materials estimator for Trinidad & Tobago.\n"
        "Return ONLY a JSON object with keys 'lines' and 'notes'.\n"
        "'lines' items must use ONLY allowed inventory keys I provide and valid units.\n"
        "If the project is a slab/driveway/pad, include reinforcement (mesh_A142_sheet or rebar_corr_3_8_m)."
    )

    user_prefix = (
        f"Parsed spec (optional): {json.dumps(spec, ensure_ascii=False)}\n\n"
        f"Allowed keys ONLY: {ALLOWED_KEYS}\n\n"
        "Respond as pure JSON. Example shape: {\n"
        "  \"lines\": [\n"
        "    {\"key\":\"sharp_sand_m3\",\"qty\":2.4,\"unit\":\"m3\"},\n"
        "    {\"key\":\"gravel_m3\",\"qty\":4.8,\"unit\":\"m3\"},\n"
        "    {\"key\":\"cement_bag\",\"qty\":18,\"unit\":\"bag\"}\n"
        "  ],\n"
        "  \"notes\":\"Short rationale and assumptions.\"\n"
        "}"
    )

    try:
        resp = _chat_completion_with_fallback(
            client,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": [
                    {"type": "text", "text": user_prefix},
                    *content  # text + images
                ]},
            ],
            timeout=60.0,
            model_kind="vision",
        )
        content_text = (resp.choices[0].message.content or "").strip()
        data = json.loads(content_text)
        cleaned = _validate_lines(data.get("lines"))
        return {"lines": cleaned, "notes": data.get("notes", "")}
    except Exception as e:
        log.exception("propose_bom_from_vision failed: %s", e)
        return {}


# --------------------------
# Staff: Purchases (OCR + Text)
# --------------------------

def _validate_purchase_lines(raw: Any) -> List[Dict[str, Any]]:
    """Validate and clean purchase lines for invoices/receipts.
    Expected fields per line: description (str), unit (allowed), qty (float>0)
    Optional: unit_price (float>=0), line_total (float>=0). If line_total missing, compute.
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for it in raw:
        if not isinstance(it, dict):
            continue
        description = (it.get("description") or "").strip()
        unit = _norm_unit_staff(it.get("unit", ""))
        qty = it.get("qty")
        try:
            qty_f = float(qty)
        except (TypeError, ValueError):
            continue
        if not description or unit not in _ALLOWED_UNITS_STAFF or qty_f <= 0:
            continue
        unit_price = it.get("unit_price")
        line_total = it.get("line_total")
        up_f: Optional[float] = None
        lt_f: Optional[float] = None
        try:
            up_f = float(unit_price) if unit_price is not None else None
        except (TypeError, ValueError):
            up_f = None
        try:
            lt_f = float(line_total) if line_total is not None else None
        except (TypeError, ValueError):
            lt_f = None
        if lt_f is None and up_f is not None:
            lt_f = up_f * qty_f
        out.append({
            "description": description,
            "unit": unit,
            "qty": round(qty_f, 4),
            **({"unit_price": round(up_f, 4)} if up_f is not None else {}),
            **({"line_total": round(lt_f, 4)} if lt_f is not None else {}),
            # Optional AI mapping to internal material key; keep if provided
            **({"material_key": it.get("material_key")} if it.get("material_key") else {}),
            **({"category": it.get("category")} if it.get("category") else {}),
        })
    return out


def propose_purchase_from_text(text: str) -> dict:
    """AI-assisted parse of free text describing a purchase.
    Returns dict with: supplier_name?, invoice_date?, lines[], tax?, total?"""
    client = _make_client()
    if not client:
        return {}

    schema_hint = (
        "Return ONLY a JSON object with keys: supplier_name?, invoice_date?, invoice_number?, "
        "currency?, lines, tax?, total?.\n"
        "lines is a list of items with fields: description (string), unit (one of yd3, m3, bag, kg, pcs, sheet, gal, lb), "
        "qty (number>0), unit_price (optional number>=0), line_total (optional number>=0).\n"
        "Prefer unit=yd3 for aggregates like sand or gravel if quantities in yards."
    )

    try:
        resp = _chat_completion_with_fallback(
            client,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": (
                    "You are a helpful assistant for staff purchase entry. "
                    + schema_hint
                )},
                {"role": "user", "content": text.strip()},
            ],
            timeout=60.0,
            model_kind="text",
        )
        content = (resp.choices[0].message.content or "").strip()
        data = json.loads(content)
        lines = _validate_purchase_lines(data.get("lines"))
        out = {
            "supplier_name": (data.get("supplier_name") or "").strip() or None,
            "invoice_date": (data.get("invoice_date") or "").strip() or None,
            "invoice_number": (data.get("invoice_number") or "").strip() or None,
            "currency": (data.get("currency") or "TTD").strip() or "TTD",
            "lines": lines,
        }
        # Optional totals
        try:
            out["tax"] = float(data.get("tax"))
        except (TypeError, ValueError):
            pass
        try:
            out["total"] = float(data.get("total"))
        except (TypeError, ValueError):
            pass
        return out
    except Exception as e:
        log.exception("propose_purchase_from_text failed: %s", e)
        return {}


def propose_invoice_from_vision(file_paths: List[str]) -> dict:
    """Extract supplier invoice details from images/PDF using a vision-capable model.
    Returns dict with supplier_name?, invoice_date?, invoice_number?, currency?, lines[], tax?, total?"""
    client = _make_client()
    if not client:
        return {}

    # Build content blocks with images (expand PDFs)
    content: List[Dict[str, Any]] = [
        {"type": "text", "text": (
            "Extract supplier invoice data. "
            "Return ONLY JSON with keys: supplier_name?, invoice_date?, invoice_number?, currency?, lines, tax?, total?. "
            "Each line has: description, unit (yd3/m3/bag/kg/pcs/sheet/gal/lb), qty, unit_price?, line_total?."
        )}
    ]
    expanded_images: List[str] = []
    for p in (file_paths or []):
        ext = os.path.splitext(p)[1].lower()
        if ext == ".pdf":
            expanded_images.extend(_pdf_to_images(p, max_pages=3))
        else:
            expanded_images.append(p)
    for img_path in expanded_images:
        data_url = _file_to_data_url(img_path)
        if not data_url:
            continue
        content.append({"type": "image_url", "image_url": {"url": data_url}})

    system = (
        "You read supplier invoices for building materials. "
        "Use yd3 for cubic yards when appropriate. "
        "Respond strictly as JSON per instructions."
    )

    try:
        resp = _chat_completion_with_fallback(
            client,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            timeout=90.0,
            model_kind="vision",
        )
        content_text = (resp.choices[0].message.content or "").strip()
        data = json.loads(content_text)
        lines = _validate_purchase_lines(data.get("lines"))
        out = {
            "supplier_name": (data.get("supplier_name") or "").strip() or None,
            "invoice_date": (data.get("invoice_date") or "").strip() or None,
            "invoice_number": (data.get("invoice_number") or "").strip() or None,
            "currency": (data.get("currency") or "TTD").strip() or "TTD",
            "lines": lines,
        }
        try:
            out["tax"] = float(data.get("tax"))
        except (TypeError, ValueError):
            pass
        try:
            out["total"] = float(data.get("total"))
        except (TypeError, ValueError):
            pass
        return out
    except Exception as e:
        log.exception("propose_invoice_from_vision failed: %s", e)
        return {}


# --------------------------
# Expenses: Text and Vision extraction
# --------------------------

def _validate_expenses(raw: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for it in raw:
        if not isinstance(it, dict):
            continue
        category = (it.get("category") or "").strip().lower()
        description = (it.get("description") or "").strip()
        try:
            amount = float(it.get("amount") or 0)
        except Exception:
            amount = 0.0
        if not category or amount <= 0:
            continue
        out.append({
            "category": category,
            "description": description,
            "amount": round(amount, 2)
        })
    return out


def propose_expenses_from_text(text: str) -> dict:
    """Parse free text into expense entries. Returns { date?, expenses: [{category, description, amount}] }"""
    client = _make_client()
    if not client:
        return {}
    schema = (
        "Return ONLY a JSON object with optional 'date' (YYYY-MM-DD) and 'expenses' list. "
        "Each expense has: category (salaries|fuel|maintenance|other), description (string), amount (number>0)."
    )
    try:
        resp = _chat_completion_with_fallback(
            client,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": schema},
                {"role": "user", "content": text.strip()},
            ],
            timeout=60.0,
            model_kind="text",
        )
        content = (resp.choices[0].message.content or "").strip()
        data = json.loads(content)
        expenses = _validate_expenses(data.get("expenses"))
        out = {"expenses": expenses}
        d = (data.get("date") or "").strip()
        if d:
            out["date"] = d
        return out
    except Exception as e:
        log.exception("propose_expenses_from_text failed: %s", e)
        return {}


def propose_expenses_from_vision(file_paths: List[str]) -> dict:
    """Extract expenses from photos/PDF receipts. Returns { date?, expenses: [...] }"""
    client = _make_client()
    if not client:
        return {}
    content: List[Dict[str, Any]] = [{"type": "text", "text": (
        "Extract company operating expenses from the attached images. "
        "Return ONLY JSON with optional 'date' and 'expenses' list where each item has: "
        "category (salaries|fuel|maintenance|other), description, amount (number>0)."
    )}]
    expanded_images: List[str] = []
    for p in (file_paths or []):
        ext = os.path.splitext(p)[1].lower()
        if ext == ".pdf":
            expanded_images.extend(_pdf_to_images(p, max_pages=3))
        else:
            expanded_images.append(p)
    for img_path in expanded_images:
        data_url = _file_to_data_url(img_path)
        if not data_url:
            continue
        content.append({"type": "image_url", "image_url": {"url": data_url}})
    try:
        resp = _chat_completion_with_fallback(
            client,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You extract expenses into strict JSON."},
                {"role": "user", "content": content},
            ],
            timeout=90.0,
            model_kind="vision",
        )
        content_text = (resp.choices[0].message.content or "").strip()
        data = json.loads(content_text)
        expenses = _validate_expenses(data.get("expenses"))
        out = {"expenses": expenses}
        d = (data.get("date") or "").strip()
        if d:
            out["date"] = d
        return out
    except Exception as e:
        log.exception("propose_expenses_from_vision failed: %s", e)
        return {}
