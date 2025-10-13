# conserv-building-solutions
An e-commerce platform for quality aggregates in Trinidad and Tobago


# Conserv BuildAdvisor — AI BOM

This version asks **OpenAI to propose a bill of materials** (BOM) first, then **prices only** the items you actually have in your CSVs.

## Run
```bash
python -m venv .venv
# Windows: .\.venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# put your OpenAI key in .env
python app.py
```

Open http://localhost:5000

## Data
Put your CSVs into `./data`:
- `buildadvisor_aggregates.csv` — keys & prices (per m³): `sand_m3, sharp_sand_m3, gravel_m3, red_sand_m3, backfill_m3, soakaway_boulders_m3`
- `steel.csv` — `name, price, size_in, grade, length_value, length_unit` (per piece/ft/m/kg) → normalized to per-meter keys like `rebar_corr_1_2_m`
- `building materials.csv` — recognizes: `cement_bag(_eco/_premium/_loose_lb)`, `block_4in/6in/8in`, `mesh_A142_sheet`, `tie_wire_kg`, `purlin_z_m/purlin_c_m`, `paint_gal`
- `lumber.csv` — optional (plywood for future)

## Flow
1. User message → `propose_bom_with_ai()` requests a **strict JSON BOM** using a controlled list of keys.
2. Server **filters & prices** only keys present in your CSV-derived map. Unknown items show as **UNPRICED** rows.
3. `expand_steps_with_ai()` writes a brief step-by-step plan (optional).

Toggle modes with `BOM_MODE` (ai/hybrid/rule) if you later want to mix formula-based items as a baseline.

## Staff ERP Module (Purchases, Billing, Printing)

Staff-only tools for supplier purchases and quick billing.

Features
- Upload supplier bills (images/PDF) → AI extraction → editable lines
- AI-assisted text entry for non-bill purchases
- Create quick customer bills from aggregates and print thermal receipts
- Purchases report with totals in yd³ (and CSV export)

Access
- Mark a user as staff by setting `is_staff = 1` in the `user` table. On first run, the app attempts to add this column automatically for SQLite.

Routes
- UI: `/staff/purchases`, `/staff/purchases/new`, `/staff/billing`, `/staff/reports/purchases`
- API: `/api/staff/purchases/extract`, `/api/staff/purchases/ai-parse-text`, `/api/staff/purchases`, `/api/staff/receipts`

Printing
- Browser print to Star TSP via `templates/print_receipt.html` using 80mm `@page` CSS. Use the system print dialog, select the Star printer, and disable headers/footers.

Environment
- Requires `OPENAI_API_KEY`. Vision/Invoice OCR uses OpenAI with image/PDF support (`pypdfium2`, `Pillow`).