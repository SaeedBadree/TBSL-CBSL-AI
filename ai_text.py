# ai_text.py
import os
import json
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

def _make_client() -> Optional[OpenAI]:
    """Create an OpenAI client. Returns None if no API key is configured."""
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        log.warning("OPENAI_API_KEY is not set")
        return None

    proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
    if proxy:
        http_client = httpx.Client(proxies=proxy, timeout=30.0)
    else:
        http_client = httpx.Client(timeout=30.0)

    return OpenAI(api_key=key, http_client=http_client)

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
        resp = client.chat.completions.create(
            model=MODEL,
            # This asks the model to emit a JSON object (no code fences, etc.)
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.2,
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
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or default_text
    except Exception as e:
        log.exception("expand_steps_with_ai failed: %s", e)
        return default_text
