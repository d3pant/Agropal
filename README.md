# Reboot the Earth — Wildfire Agricultural Advisory System

An AI-powered wildfire monitoring and agricultural advisory system for farms anywhere in the United States. The farmer pins their location during onboarding — all agents use that pin as the source of truth. Detects fire threats in real time and activates downstream agents (Crop, Livestock, ERPC — the Economic Reporting & Policy Coordinator) when risk thresholds are crossed.

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Agents](#agents)
3. [Frontend Dashboard](#frontend-dashboard)
4. [Backend API](#backend-api)
5. [End-to-End Pipeline](#end-to-end-pipeline)
6. [n8n Webhook Integration](#n8n-webhook-integration)
7. [Multilingual Action Briefing](#multilingual-action-briefing)
8. [Running the System](#running-the-system)
9. [External APIs & Data Sources](#external-apis--data-sources)
10. [Aid & Recovery Programs](#aid--recovery-programs)
11. [Science & Threshold References](#science--threshold-references)
12. [License](#license)

---

## System Overview

Six agents in a linear pipeline with a parallel middle stage. Dormant until a real threat exists, then activates fully.

```
┌─────────────────┐
│  FORECASTING    │  ← Always running
│     AGENT       │
└────────┬────────┘
         │ Gate condition met → wakes both simultaneously
    ┌────┴────┐
    ▼         ▼
┌───────┐ ┌──────────┐
│ CROP  │ │LIVESTOCK │  ← Parallel (asyncio); communicate via files
│ AGENT │ │  AGENT   │
└───┬───┘ └────┬─────┘
    └─────┬─────┘
          ▼
┌──────────────────────┐
│   ERPC: Econ ▸ Policy▸│  ← Reads live crop + livestock outputs;
│         Insurance     │     fills CCC-576 PDF
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│  REPORT AGENT (PDF)  │  ← Combined briefing + go-bag checklist;
│  + 10 languages      │     translation via Google + Unicode fonts
└──────────┬───────────┘
           ▼
   FARMER DASHBOARD ◂──── n8n webhook (email/Slack/SMS via your workflow)
```

The whole chain runs in one HTTP call: **`POST /api/run-pipeline`** triggers forecaster → (crop ‖ livestock) → econ → insurance → report sequentially, in ~60–180s end-to-end.

Threat levels used system-wide: `GREEN → WATCH → WARNING → CRITICAL → EMERGENCY`

### File Structure

| Path | Description |
|------|-------------|
| `forecaster/forecaster.py` | Forecasting agent — threat assessment, gate condition, wake-up packet |
| `erpc/econ_agent.py` | ERPC econ module — financial exposure, ROI action ranking |
| `erpc/policy_agent.py` | ERPC policy module — aid/grant eligibility engine |
| `erpc/insurance_agent.py` | ERPC insurance module — fills official USDA CCC-576 Notice of Loss form |
| `erpc/report_agent.py` | Action briefing PDF — combines all agent outputs, 10 languages, evacuation checklist |
| `forecaster/models/spread_model.py` | Rothermel fire spread model + Anderson ellipse |
| `forecaster/data_sources/` | Data fetchers: NASA FIRMS, NDVI, Open-Meteo, NOAA, SDG&E |
| `forecaster/predictors/` | WIFIRE + Pyrecast spread prediction integrations |
| `forecaster/config/farm_config.json` | Farm profile: location, thresholds, zones, crops, animals |
| `forecaster/output/` | Runtime outputs: `status.json`, `wake_up_packet.json`, `econ_report.json`, `policy_report.json`, `ccc_576_filled.pdf`, `action_briefing[_lang].pdf` |
| `forecaster/forms/` | Bundled government form templates: `ccc_576.pdf` (official USDA Notice of Loss) |
| `crop_agent/crop_agent.py` | Groq llama-3.3-70b crop agent — field decisions, fire reduction, hydration, economic impact |
| `Livestock/livestock_agent.py` | Async OSRM-routed evacuation planner — per-pen decisions, transport pool, cost optimization |
| `backend/main.py` | FastAPI server — agent orchestration, pipeline endpoint, briefing/insurance/report endpoints |
| `backend/static/setup.html` | 3-step farm onboarding form (location, livestock, crops) |
| `backend/static/dashboard.html` | Live threat dashboard with map, threat gauge, agent panels |
| `backend/static/financial.html` | Financial overview: exposure, ROI actions, aid programs, insurance form download |
| `backend/static/briefing.html` | Plan tab: download briefing PDF in 10 languages + n8n webhook send |
| `.env.example` | Template for `.env` with all required API keys |

---

## Agents

### Forecasting Agent

`forecaster/forecaster.py`

**Stage 1 (passive):** Checks nearest fire distance on a schedule. FWI and NDVI are recorded as context but do not affect the threat level. No downstream agents active.

**Gate condition:** Threat crosses WARNING or above → activates Stage 2.

**Stage 2 (active):** Full pipeline — fire spread prediction, per-zone time-to-impact, writes `wake_up_packet.json` and wakes Crop + Livestock agents simultaneously.

Update intervals by threat level: GREEN=720min, WATCH=120min, WARNING=30min, CRITICAL=15min, EMERGENCY=5min.

**Threat level — distance only (`_fire_distance_threat`):**

| Fire distance | Threat level |
|---------------|-------------|
| No fire detected | GREEN |
| > 200 km | GREEN |
| 100–200 km | WATCH |
| 50–100 km | WARNING |
| ≤ 50 km | CRITICAL |
| ≤ 75 km (hard floor) | CRITICAL |

FWI and NDVI are displayed on the dashboard as weather context but never escalate the threat level. This prevents false alarms from high fire-weather conditions when no fire is actually nearby.

---

### Econ Agent (ERPC)

`erpc/econ_agent.py` → `forecaster/output/econ_report.json`

Runs during Stage 2. Each cycle computes total financial exposure and produces a prioritized ROI action queue for the farmer dashboard.

**Financial loss categories:**

| Category | What It Covers | Source |
|----------|---------------|--------|
| Crop loss (confirmed) | ABANDON fields — loss locked in | `crop_agent/crop_agent_output_*.json` → `economic_impact.crop_destructions` |
| Crop loss (recoverable) | HARVEST NOW / PARTIAL HARVEST fields — preventable with action | Same + `field_decisions.maturity_pct` |
| Livestock at risk | `total_head × value/head × (1 − evacuated_pct)` | `Livestock/erpc_message.json` → `animal_valuation_at_risk`, `total_animals_at_risk` |
| Opportunity cost | 1 lost season × price/acre × acres for ABANDON; partial season for PARTIAL HARVEST | `economic_impact.price_per_acre_usd` + `field_decisions.maturity_pct` |

Loss figure used throughout: `confidence_adjusted_loss_usd` — conservative floor, not best-case.

Not yet computable: soil rehabilitation, replanting cost, market timing losses, labor disruption.

**ROI formula:**
```
ROI = confidence_adjusted_loss_avoided / estimated_action_cost
```

**Action types:**

| Action | Loss Avoided | Cost Basis | Time Constraint |
|--------|-------------|-----------|-----------------|
| HARVEST NOW | Full `confidence_adjusted_loss_usd` | Labor rate × harvest hours | Before `fire_arrival_hours` |
| PARTIAL HARVEST | `confidence_adjusted_loss_usd × maturity_pct` | Labor rate × hours × maturity fraction | Before `fire_arrival_hours` |
| TRANSPLANT | Seedling replacement value | Labor rate × `labor_hours_needed` | Before `time_window` |
| WET FIREBREAK | `confidence_adjusted_loss_usd` of protected field | Firebreak cost × field acres | IMMEDIATE or SCHEDULED |
| EVACUATE LIVESTOCK | `Livestock/erpc_message.json` at-risk value | `transport_costs_usd` from Livestock Agent | Zone time-to-impact |

Feasibility gates (hard — action dropped if failed): `feasible_with_farm_resources = False`, insufficient time window, or field decision is ABANDON.

**Hardcoded cost assumptions** (all logged in output JSON under `cost_assumptions_used`):

| Constant | Value | Unit | Replace With |
|----------|-------|------|--------------|
| Harvest labor rate | $25 | $/hr | USDA regional farm wage data |
| Harvest time | 4 | hrs/acre | Per-crop estimate from Crop Agent |
| Firebreak cost | $150 | $/acre | CAL FIRE cost estimates |
| Livestock transport | Live | $/total | From `Livestock/erpc_message.json` `transport_costs_usd` |
| Livestock value/head | Live | $/head | From `animal_valuation_at_risk / total_animals_at_risk` |
| Livestock head count | Live | head | From `Livestock/erpc_message.json` `total_animals_at_risk` |
| Transplant seedling value | $800 | $/acre | Nursery price index |
| Opportunity cost horizon | 1 | season | Agronomist input |

When the crop agent hasn't run yet (threat level GREEN, or Groq unavailable), the econ agent builds a minimal fallback from `crop_agent/farm_fields.json` using the farmer's actual field IDs and crop types — no hardcoded mock fields. Stale crop outputs are filtered at the API level to strip any field_ids not in the current `farm_fields.json`.

---

### Insurance Agent (ERPC)

`erpc/insurance_agent.py` → `forecaster/output/ccc_576_filled.pdf`

Runs post-event. Reads `econ_report.json` and `status.json` and fills the official USDA **CCC-576 (Notice of Loss)** PDF form using `pypdf`. The CCC-576 is the primary form for ELAP, LFP, LIP, and NAP claims — it must be filed within 30 days of the loss event at the local FSA office.

The agent pre-populates all fields it has data for and leaves the rest blank so the farmer can complete them on-site. The filled form is print-ready.

**What gets pre-filled:**

| CCC-576 Section | Fields Pre-filled | Fields Left Blank (farmer fills) |
|----------------|------------------|----------------------------------|
| Part A Header (Items 1–6) | FSA office address, crop year, producer name/location, state/county FIPS, disaster type "Wildfire", disaster dates, crop name, intended use | Crop variety/type, planting period |
| Part A Acreage (Items 7–8) | Farm ID, intended acres, planted acres, disaster-affected acreage (up to 3 crops) | NAP unit numbers, prevented-planted |
| Part B Production (Items 11–29) | Crop name, producer share (100%), acreage, loss description, salvage value (up to 3 crops) | Pay codes, stage, actual production records |
| Part C Inventory (Items 32–37) | Crop value before disaster, value after (ABANDON = $0), salvage estimate (up to 3 crops) | Ineligible value (FSA fills) |
| Part D Forage (Items 38–48) | — | All (needs Livestock Agent) |
| Signatures | — | In-person at FSA office |

56 of 97 mapped fields pre-filled from system data.

**Form source:** official USDA CCC-576 from https://www.farmers.gov/sites/default/files/documents/ccc-576.pdf, bundled at `forecaster/forms/ccc_576.pdf` (181 AcroForm fields, 2 pages).

---

### Policy Agent (ERPC)

`erpc/policy_agent.py` → `forecaster/output/policy_report.json`

Runs post-event (after all-clear). Evaluates farm eligibility for 25+ wildfire recovery programs across USDA, FEMA, SBA, and CA state agencies. Outputs a ranked list (confirmed → likely → check_required → ineligible) with deadlines, required documents, and direct links.

Key logic: FEMA Disaster Declarations API is queried first — that single boolean gates whether FEMA IA, FEMA HMGP, FSA Emergency Loans, and SBA EIDL are `confirmed` or `check_required`. Loss flags (`crop_loss`, `livestock_loss`, `economic_injury`) are derived live from `econ_report.json`. **Tavily web-search enrichment** scores acceptance probability for each program based on current eligibility chatter; all other eligibility fields use farm profile constants.

---

### Report Agent

`erpc/report_agent.py` → `forecaster/output/action_briefing[_<lang>].pdf`

Final stage of the pipeline. Pulls every other agent's output and renders a single comprehensive PDF the farmer (or stakeholder) can read on a phone or print. Produced in **English plus 9 other languages** on demand.

**Sections (text-heavy prose, not dashboards — written so Google Translate produces high-quality output):**

1. Threat snapshot — level, nearest fire, FWI, wind, time-to-impact
2. Livestock plan — per-pen evacuation decisions, evac sites, cost optimization
3. Crop plan — field decisions, hydration schedule, economic impact
4. Financial snapshot — total exposure, ROI-ranked actions, blocked actions
5. Aid & insurance — CCC-576 reminder, top eligible aid programs
6. **Evacuation Go-Bag Checklist** — 6 categories (Personal, Documents, Animals, Farm Records, Vehicle Prep, Don't Forget) with ~30 actionable items farmers should grab before leaving the property
7. Emergency contacts — 911, CAL FIRE, FSA office, assigned evac sites

**Translation pipeline:**

| Concern | Solution |
|---|---|
| 10 target languages | `deep-translator` — free Google web endpoint, no API key, ~80s per non-English render |
| CJK / Devanagari / Vietnamese render as `■` boxes | Per-language Unicode font registration: `STSong-Light` (zh-CN), `HYSMyeongJo-Medium` (ko), `Arial Unicode.ttf` (vi/hi/ar/everything else); Helvetica for en/es/fr/pt/tl |
| Arabic shows isolated letters in wrong direction | `arabic-reshaper` (joins to connected forms) + `python-bidi` (visual reorder) before rendering; HTML markup stripped pre-bidi to keep XML well-formed |
| All-caps "CRITICAL" mistranslated as "wonderful" in Vietnamese | Title-case the threat level word inside prose so translator reads it as an adjective, not an exclamation |

Supported languages: `en`, `es`, `zh-CN`, `vi`, `tl`, `ko`, `ar`, `hi`, `fr`, `pt`.

---

## Frontend Dashboard

Five-tab single-app layout. All pages share the same cream/green design system: Inter variable font, `--cream:#faf9f5` background, `--green:#2d6a4f` accent, 56px sticky header, and an animated SVG film-grain overlay at 14% opacity. A yellow setup-incomplete banner appears on every page if `.farm_setup_done` is absent.

**Navigation tabs** (same header bar across all pages): Overview | Livestock | Crops | Financial | Action Plan

The header shows the farm name chip (name · acres · pen count) pulled live from `/api/farm-profile`, plus the current threat level badge (color-coded by level) and a **↻ Refresh** button that runs the full pipeline.

---

### Setup — `/static/setup.html`

Three-step onboarding wizard:
1. Farm name, interactive Leaflet map pin (**required** — click the map to place it; the pin coordinates are the farm's canonical location used by all agents), total acres
2. Pen inventory — species, head count, age class, health status, notes per pen
3. Field inventory — crop type, acres, planting date per field

Posts to `POST /api/setup` which writes `farm_config.json`, `farm_profile.json`, `farm_fields.json` and touches `.farm_setup_done`. All agents read the farm lat/lon from `farm_config.json` at runtime — no hardcoded coordinates anywhere in the codebase.

---

### Dashboard — `/static/dashboard.html` (root `/`)

Split-screen hero: Leaflet ESRI satellite map (left) + threat stats panel (right). Below the hero scrolls a bottom grid.

**Map:** VIIRS fire markers colored by intensity, Rothermel spread ellipses at 6h/12h/24h, farm location pin, farm radius circle scaled to actual acreage, per-pen markers, evacuation site markers. Map legend in bottom-left corner.

**Threat stats panel (right column):**
- Threat level hero — 48px/900-weight display text, color-coded by level
- Fire Risk Score gauge — dynamically computed via `computeRiskScore()`:
  - FWI component: up to 55 pts (`fwi / 30 × 55`, capped)
  - Humidity bonus: 10 pts if < 15%, 7 pts if < 25%, 3 pts if < 40%
  - Wind bonus: 8 pts if > 50 km/h, 5 pts if > 30, 3 pts if > 15
  - Proximity pts: 30 at ≤ 10 km grading down to 1 at > 500 km
  - Sum capped at 100 — never hardcoded to threat band
- Key numbers: nearest fire distance, estimated time to reach farm (< 24h / < 72h / 72h+ with context-aware wording), FWI index
- Environmental row: wind speed + direction, humidity, temperature
- `gate_condition_reason` sub-text explains exactly which signal triggered the alert

**Bottom grid — three cards:**
- **Act Now** — crop field decisions from Crop Agent (HARVEST NOW / PARTIAL HARVEST / TRANSPLANT / MONITOR / ABANDON)
- **Your Farm** — livestock pen status from Livestock Agent (per-pen evacuation decision, transport needed, OSRM-routed evac sites)
- **What's at Risk** — financial exposure summary linking to Financial tab

**Threat level logic** (distance-only — Forecaster):
| Fire distance | Level |
|---------------|-------|
| No fire / > 200 km | GREEN |
| 100–200 km | WATCH |
| 50–100 km | WARNING |
| ≤ 50 km or ≤ 75 km hard floor | CRITICAL |

FWI and NDVI are shown as weather context on the dashboard but do not affect the threat level. GREEN state shows a clean "All Clear — No Action Needed" UI across all tabs; agents stay dormant until WARNING or above.

---

### Financial — `/static/financial.html`

Full financial overview page. Reads `/api/econ`, `/api/policy`, `/api/insurance/status`.

**KPI row (4 cards):**
- Total Exposure — confidence-adjusted sum if no action taken
- Crop Loss — confirmed abandoned + recoverable fields combined
- Livestock at Risk — head count × value/head for unevacuated animals
- Aid Programs — count of confirmed-eligible programs of total evaluated

**Charts (Chart.js 4.4):**
- **Exposure donut** — crop confirmed / crop recoverable / livestock / opportunity cost by USD segment
- **Action ROI horizontal bar** — feasible actions sorted by ROI descending; color-coded IMMEDIATE (red) / HIGH (orange) / SCHEDULED (green)
- **Cost to Act vs Loss Avoided grouped bar** — both feasible and infeasible actions side by side in USD
- **Aid Program Eligibility donut** — confirmed / likely / check_required / ineligible counts

**Program Deadlines timeline** — all aid programs with hard ISO dates, sorted by urgency. Days-remaining chips: red ≤ 30 days, amber ≤ 90, green otherwise.

**Eligible Aid Programs table** — confirmed + likely + non-Grants.gov check_required programs. Columns: name/agency, eligibility status pill, deadline chip, estimated value, Apply link. Ineligible programs hidden by default.

**CCC-576 Insurance Card:**
- PDF preview tile (paper mockup with "PRE-FILLED" stamp)
- Status pill: READY TO FILE (green, < 24h old) or STALE — RE-RUN (amber)
- Meta grid: agency, 30-day filing deadline (urgent red), FSA office address, last generated timestamp
- Fields progress bar: filled / total AcroForm fields with percentage, plus plain-English description of what was pre-filled vs what the farmer must complete in person
- **Download CCC-576 PDF** CTA — streams directly from `/api/insurance/pdf`
- **Regenerate** button — calls `POST /api/insurance/run` in-place without leaving the page

---

### Action Plan — `/static/briefing.html`

PDF delivery page for the action briefing generated by the Report Agent.

**Hero card — two side-by-side panels:**
- **Download PDF panel** — language selector (populated from `/api/report/languages`), Download button calls `GET /api/report/pdf?lang=` and auto-generates if missing
- **Send to stakeholder panel** — recipient email input, Send button posts to `POST /api/report/email` which fires the n8n webhook with the PDF as base64 plus a structured summary object; status feedback shown inline

**What's in this report card** — numbered grid showing the 6 sections the PDF contains:
1. Threat snapshot (level, fire, FWI, wind, time-to-impact)
2. Livestock plan (per-pen decisions, evac sites, costs)
3. Crop plan (field decisions, hydration, economic impact)
4. Financial snapshot (exposure, ROI actions, blocked actions)
5. Aid & insurance (CCC-576 reminder, top programs, deadlines)
6. Evacuation go-bag checklist (personal docs, animal records, vehicle prep, contacts)

**Go-Bag Checklist card** — printable inline version of the checklist with checkboxes across 6 categories (~30 items), matching what's in the PDF.

---

The emergency bar is the same across pages — single **↻ Refresh** button calls `/api/run-pipeline`. Per-agent buttons removed in favor of the orchestrator.

---

## Backend API

All endpoints are FastAPI handlers in `backend/main.py`. Read endpoints return cached JSON; run endpoints kick off subprocesses.

### Read endpoints

| Method | Path | Returns |
|--------|------|---------|
| GET | `/api/setup/status` | `{complete: bool}` |
| GET | `/api/farm-profile` | farm_profile.json contents (pens, infrastructure, total_acres) |
| GET | `/api/status` | latest forecaster status.json (threat, fire, weather, gate, spread) |
| GET | `/api/fires` | live NASA FIRMS GeoJSON for California |
| GET | `/api/spread?fire_lat=&fire_lon=&fire_frp=` | Rothermel ellipses at 6h/12h/24h |
| GET | `/api/impact?target_lat=&target_lon=` | nearest fire + haversine distance + time-to-impact |
| GET | `/api/livestock/status` | livestock_status.json |
| GET | `/api/crop/status` | latest crop output (normalized: `field_decisions`, `fire_reduction`, `economic_impact`, `hydration_strategy`) |
| GET | `/api/econ` | econ_report.json (exposure + ROI action queue) |
| GET | `/api/policy` | policy_report.json (eligible aid programs) |
| GET | `/api/insurance/status` | metadata for filled CCC-576 (filename, size, fields-filled count, fsa_office, deadline_days) |
| GET | `/api/insurance/pdf` | streams filled CCC-576 PDF as download |
| GET | `/api/report/status` | metadata for English action briefing |
| GET | `/api/report/languages` | dropdown source: `[{code, label}, …]` |
| GET | `/api/report/pdf?lang=` | streams briefing PDF (auto-generates if missing) |

### Action endpoints

| Method | Path | Effect |
|--------|------|--------|
| POST | `/api/setup` | Writes farm_config.json, farm_profile.json, farm_fields.json from form data |
| POST | `/api/run-forecaster` | Runs forecaster cycle alone — writes status.json |
| POST | `/api/livestock/run` | Syncs forecaster data, runs livestock_agent.py subprocess |
| POST | `/api/crop/run` | Runs crop_agent.py subprocess (Groq LLM) |
| POST | `/api/insurance/run` | Generates filled CCC-576 PDF |
| POST | `/api/report/run?lang=` | Generates action briefing in target language |
| POST | `/api/report/email` | Triggers n8n webhook with PDF + summary (see below) |
| **POST** | **`/api/run-pipeline`** | **Orchestrated end-to-end run — see next section** |

---

## End-to-End Pipeline

`POST /api/run-pipeline` is the single button-press endpoint that runs the whole chain:

```
forecaster_cycle()
   │ writes status.json
   ▼
asyncio.gather(                          ← parallel
    crop_subprocess(),                   ← Groq LLM, ~30-60s
    livestock_subprocess(),              ← OSRM routes + CalOES, ~30-60s
)
   │ outputs land in disk
   ▼
econ_subprocess()                        ← reads live crop + livestock output
   │ writes econ_report.json
   ▼
insurance_subprocess()                   ← fills CCC-576 PDF
   │
   ▼
report_subprocess(lang="en")             ← combined briefing PDF
   │
   ▼
returns {forecaster, crop, livestock, econ, insurance, report, data_sources}
```

Total wall time ~60-180s depending on Groq latency and OSRM routing. Each subprocess is independent and graceful on failure — if Groq rate-limits, crop returns `{"error": …}`, econ builds a minimal fallback from `farm_fields.json` (no hardcoded mock data), the rest of the pipeline keeps going.

**Crop output filename quirk:** the crop agent writes `crop_agent/output_<timestamp>.json` (not `crop_agent_output_*.json`). The econ agent's loader globs both patterns; the on-disk path is what's actually used.

---

## n8n Webhook Integration

The Send-briefing button on `/static/briefing.html` POSTs to `/api/report/email`, which fires a **GET request to your n8n webhook** with the briefing payload. n8n then does whatever you've wired (Gmail node, Slack node, SMS, Notion, etc.) — the backend's job is just to package and deliver.

Default URL is hardcoded in `backend/main.py` (`N8N_WEBHOOK_DEFAULT`); override with `N8N_WEBHOOK_URL` in `.env`.

**Request shape** — query params (small) + JSON body (full payload):

```
GET https://<your-n8n-host>/webhook/<id>
    ?recipient=name@example.com&language=en&filename=action_briefing.pdf

Body:
{
  "recipient":   "name@example.com",
  "subject":     "Wildfire Action Briefing — English",
  "language":    "en",
  "note":        "",
  "filename":    "action_briefing.pdf",
  "pdf_base64":  "JVBERi0xLjQK…",        ← decode in n8n for an attachment
  "generated_at":"2026-05-09T10:11:00Z",
  "summary": {
    "farm_name": "…", "threat_level": "CRITICAL",
    "nearest_fire": { "name": "…", "distance_km": 35.97, "frp_mw": 4.53 },
    "time_to_impact_hours": null, "fwi": 0.61, "wind_kmh": 2.5,
    "animals_at_risk": 50, "animals_can_evacuate": 50, "livestock_value_usd": 72500,
    "total_exposure_usd": 72500,
    "crop_decisions": [{"field":"F1","crop":"avocado","decision":"HARVEST NOW"}],
    "top_actions":    [{"action":"…","urgency":"HIGH","roi":96.7,"loss_avoided_usd":72500,"cost_usd":750}],
    "top_aid_programs":[{"name":"…","agency":"USDA-FSA","deadline":null,"status":"confirmed"}],
    "insurance_pdf_ready": true
  }
}
```

The summary is intentionally compact — drops polygon coords, route waypoints, full LLM rationale. n8n receives ~14 headline fields plus the rendered PDF.

In your n8n workflow, after the Webhook trigger:
1. **Code node** decodes `pdf_base64` to binary: `return [{ binary: { data: { data: $json.body.pdf_base64, mimeType: 'application/pdf', fileName: $json.body.filename } } }];`
2. **Gmail / SMTP / SendGrid node** uses `{{$json.body.recipient}}`, `{{$json.body.subject}}`, `{{$json.body.body}}` and the binary attachment

Test webhooks (`/webhook-test/...`) only accept one call per "Listen for test event" arming. Use the production URL (`/webhook/...` with the workflow Active) for repeated firing.

---

## Multilingual Action Briefing

The briefing PDF is generated in **10 languages**: English, Spanish, Chinese (Simplified), Vietnamese, Tagalog (Filipino), Korean, Arabic, Hindi, French, Portuguese. Selected on the Plan page; passed as `?lang=…` to the API.

**Translation:** `deep-translator` — free Google web endpoint, ~80s per non-English render, ~75–110 string cache hits per language.

**Font handling:**

| Language(s) | Font | Why |
|---|---|---|
| en, es, fr, pt, tl | Helvetica (default) | Latin-1 supplement covers all required glyphs |
| zh-CN | `STSong-Light` (built-in CID) | Simplified Chinese; no external file needed |
| ko | `HYSMyeongJo-Medium` (built-in CID) | Korean Hangul; no external file needed |
| vi, hi, ar, others | `Arial Unicode.ttf` (system) | ~50,000 glyphs; covers Latin Extended Additional, Devanagari, Arabic, etc. |

**Arabic RTL pipeline:** `arabic-reshaper` joins isolated letters → connected forms; `python-bidi` applies the Unicode bidi algorithm for correct visual order. HTML `<b>` markup is stripped pre-bidi (it would break char-reordering and produce malformed XML).

**Why prose, not tables:** the report deliberately uses flowing paragraphs and bulleted sentences instead of KPI grids. Translators give much better output when each cell carries full sentence context — for example, "pen" alone translates to "Bolígrafo" (writing pen) in Spanish, but "the per-pen evacuation plan" correctly resolves to "el plan de evacuación por corral" (livestock pen).

---

## Running the System

### Server (recommended)

```bash
# Run the whole stack in one command — all agents are exposed as endpoints
uvicorn backend.main:app --port 8000 --app-dir /path/to/Reeboot_th_earth

# Visit http://localhost:8000/  (redirects to setup.html if not configured)
```

### CLI agents (for debugging / one-off runs)

```bash
# Forecaster
cd forecaster
python forecaster.py --scenario fire_threat       # mock
python forecaster.py --use-real-data              # live NASA FIRMS + Open-Meteo

# Econ agent
python -m erpc.econ_agent            # reads live crop + livestock output
python -m erpc.econ_agent --dry-run  # mock data

# Policy agent
python -m erpc.policy_agent          # live FEMA + Grants.gov + Tavily
python -m erpc.policy_agent --dry-run

# Insurance agent
python -m erpc.insurance_agent       # writes ccc_576_filled.pdf

# Report agent (action briefing)
python -m erpc.report_agent                  # English
python -m erpc.report_agent --lang es        # Spanish
python -m erpc.report_agent --lang zh-CN     # Chinese
python -m erpc.report_agent --lang ar        # Arabic (RTL applied)
```

### Environment

API keys go in `.env` at the **project root** (not inside `forecaster/`). Copy from `.env.example`:

```bash
cp .env.example .env
# Edit with real values
```

Required: `NASA_FIRMS_API_KEY`, `GROQ_API_KEY`. Optional: `TAVILY_API_KEY` (policy enrichment), `USDA_NASS_API_KEY` (crop prices), `OPENET_API_KEY` (soil moisture), `N8N_WEBHOOK_URL` (override default).

### Python dependencies

```
fastapi, uvicorn, httpx, python-dotenv, pydantic        # web stack
pypdf, reportlab                                        # PDF I/O + generation
deep-translator                                         # report translation
arabic-reshaper, python-bidi                            # Arabic RTL handling
groq                                                    # crop agent LLM
```

---

## External APIs & Data Sources

### Fire Detection & Weather

| # | Source | Data Used | Access | Key Required |
|---|--------|----------|--------|-------------|
| 1 | [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov) | VIIRS S-NPP active fire detections (375m, ~4x/day) | Free — register at earthdata.nasa.gov | Yes — MAP KEY |
| 2 | [Open-Meteo](https://open-meteo.com) | FWI, wind, temperature, humidity, soil moisture (hourly) | Free | No |
| 3 | [SDG&E FPI](https://www.sdge.com/wildfire-safety) | Localized FWI for San Diego County (2x/day) | Utility partner agreement | Yes — stubbed |
| 4 | [NASA AppEEARS / NDVI](https://appeears.earthdatacloud.nasa.gov) | MODIS MOD13Q1 NDVI 16-day composite at 250m | Free — NASA Earthdata account | Yes |
| 5 | [NOAA Weather API](https://www.weather.gov/documentation/services-web-api) | Hourly wind, temperature, humidity forecast | Free | No |
| 6 | [WIFIRE Firemap](https://wifire.ucsd.edu) | Real-time fire spread direction, speed, community proximity | Research affiliation required | Yes — stubbed |
| 7 | [Pyrecast](https://pyrecast.org) | 14-day ensemble fire spread (200 members, async) | WIFIRE Lab approval required | Yes — stubbed |
| 8 | [Copernicus EFFIS](https://effis.jrc.ec.europa.eu) | Fire danger forecast, global coverage (daily) | Free — EU Copernicus account | No |

### Policy & Aid

| # | Source | Data Used | Access | Key Required |
|---|--------|----------|--------|-------------|
| 9 | [OpenFEMA Disaster Declarations](https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries) | Fire declarations by county — gates program eligibility | Free | No |
| 10 | [Grants.gov Search API](https://api.grants.gov/v1/api/search2) | Live federal grant opportunities (wildfire + agriculture) | Free | No |
| 11 | [Farmers.gov Program Deadlines](https://www.farmers.gov/working-with-us/program-deadlines) | FSA enrollment windows — scraped weekly to local cache | Public HTML | No |

### Visualization

| Library | Version | License | URL |
|---------|---------|---------|-----|
| Leaflet.js | 1.9.4 | BSD 2-Clause | https://leafletjs.com |
| CartoDB Dark Matter Tiles | — | CC BY 3.0 | https://carto.com/basemaps/ |

### Academic Citations

> Giglio, L., Schroeder, W., & Justice, C. O. (2016). The collection 6 MODIS active fire detection algorithm. *Remote Sensing of Environment*, 178, 31–41. https://doi.org/10.1016/j.rse.2016.02.054

> Zippenfenig, P. (2023). Open-Meteo.com Weather API. Zenodo. https://doi.org/10.5281/zenodo.7970649

> Didan, K. (2021). *MODIS/Terra Vegetation Indices 16-Day L3 Global 250m SIN Grid V061*. NASA LP DAAC. https://doi.org/10.5067/MODIS/MOD13Q1.061

> Altintas, I. et al. (2015). Towards integrated cyberinfrastructure for WIFIRE. *Workshop on Big Data from Stream to Knowledge*. https://doi.org/10.1145/2834976.2834985

> Van Wagner, C. E. (1987). *Development and structure of the Canadian Forest Fire Weather Index System* (Tech. Report 35). Canadian Forestry Service.

---

## Aid & Recovery Programs

Programs evaluated by the Policy Agent for every post-event report. All links are live.

### USDA — Farm Service Agency (FSA)

| Program | What It Covers | Link |
|---------|---------------|------|
| Emergency Livestock Relief Program (ELRP) | Feed cost losses from wildfire; auto-payment if LFP on file | https://www.fsa.usda.gov/resources/disaster-recovery/emergency-livestock-relief-program-elrp |
| Emergency Assistance for Livestock (ELAP) | Grazing/feed losses, water hauling, livestock transport | https://www.fsa.usda.gov/programs-and-services/disaster-assistance-program/emergency-assist-for-livestock-honey-bees-fish/index |
| Livestock Forage Disaster Program (LFP) | Forage losses; gateway to ELRP auto-payment | https://www.fsa.usda.gov/resources/disaster-recovery/livestock-forage-disaster-program-lfp |
| Livestock Indemnity Program (LIP) | Livestock deaths above normal mortality | https://www.fsa.usda.gov/programs-and-services/disaster-assistance-program/livestock-indemnity/index |
| Noninsured Crop Disaster Assistance (NAP) | Crop losses for producers without federal crop insurance | https://www.fsa.usda.gov/programs-and-services/disaster-assistance-program/noninsured-assistance/index |
| Emergency Conservation Program (ECP) | Fencing, water restoration, debris removal | https://www.fsa.usda.gov/programs-and-services/conservation-programs/emergency-conservation/index |
| Emergency Forest Restoration Program (EFRP) | Non-industrial private forest restoration | https://www.fsa.usda.gov/programs-and-services/conservation-programs/emergency-forest-restoration/index |
| Supplemental Disaster Relief Program (SDRP) | Crop revenue losses from 2023/2024 events | https://www.fsa.usda.gov/resources/programs/20232024-supplemental-disaster-assistance |
| FSA Emergency Farm Loans | Up to $500,000; requires federal disaster declaration | https://www.fsa.usda.gov/programs-and-services/farm-loan-programs/emergency-farm-loans/index |

### USDA — NRCS

| Program | What It Covers | Link |
|---------|---------------|------|
| EQIP — Wildfire | Conservation practices on cropland, rangeland, private forest | https://www.nrcs.usda.gov/programs-and-initiatives/eqip-environmental-quality-incentives |
| Emergency Watershed Protection (EWP) | Debris removal, bank reshaping, levee repair, reseeding | https://www.nrcs.usda.gov/programs-and-initiatives/ewp-emergency-watershed-protection-program |

### FEMA

| Program | What It Covers | Link |
|---------|---------------|------|
| Individual Assistance (IA) | Home/property repair grants; requires Presidential declaration | https://www.disasterassistance.gov |
| Fire Management Assistance Grant (FMAG) | State/tribal fire mitigation; activates downstream programs | https://www.fema.gov/assistance/public/fire-management-assistance |
| Hazard Mitigation Grant Program (HMGP) | Long-term mitigation (firebreaks); up to 12 months post-declaration | https://www.fema.gov/grants/mitigation/hazard-mitigation |

### SBA

| Program | What It Covers | Link |
|---------|---------------|------|
| Economic Injury Disaster Loans (EIDL) | Up to $2M for cash flow losses for small agricultural businesses | https://www.sba.gov/funding-programs/disaster-assistance |

### UN / International

| Program | What It Covers | Link |
|---------|---------------|------|
| FAO Global Fire Management Hub | Coordination body for international farmers; not direct US aid | https://www.fao.org/partnerships/fire-hub/en |
| Green Climate Fund (GCF) via FAO | Climate resilience funding via national government applications | https://www.greenclimate.fund/ae/fao |

### California State

| Program | Agency | What It Covers | Link |
|---------|--------|---------------|------|
| Emergency Relief Programs | CDFA | CA wildfire farm losses; requires governor's declaration | https://www.cdfa.ca.gov/grants/ |
| Forest Health Grants | CAL FIRE | Reforestation for private forest landowners | https://www.fire.ca.gov/grants |
| Office of Emergency Food and Farming Infrastructure | CDFA | Small/mid-scale farms with food system disruption | https://www.cdfa.ca.gov/oefi/ |
| Disaster Unemployment Assistance | CA EDD | Self-employed farmers who lost work from declared disaster | https://edd.ca.gov/en/unemployment/disaster/ |

---

## Science & Threshold References

### Canadian Forest Fire Weather Index (FWI) System

The FWI system is the international standard for fire weather. Six components:

| Component | Measures |
|-----------|---------|
| FFMC | Moisture of fine fuels (litter, grass) |
| DMC | Moisture of loosely compacted organic layers |
| DC | Seasonal drought effect on deep organic layers |
| ISI | Expected rate of fire spread |
| BUI | Total fuel available for combustion |
| FWI | Overall fire intensity potential (0–180) |

### Fire Spread Model

`forecaster/models/spread_model.py` implements the Rothermel (1972) rate-of-spread model with Anderson (1985) ellipse geometry, calibrated for SoCal chaparral fuel types.

### Threshold Sources

FWI and fire distance thresholds in `forecaster/config/farm_config.json` are derived from:
- SDG&E Wildfire Mitigation Plan 2023–2025
- CAL FIRE Fire Hazard Severity Zone classifications
- USDA Forest Service Fire Danger Rating System (FDRS)

---

## License

Code: MIT
Data: Subject to individual provider licenses. NASA and NOAA data are U.S. Government public domain. Open-Meteo data is CC BY 4.0. CartoDB tiles are CC BY 3.0.
