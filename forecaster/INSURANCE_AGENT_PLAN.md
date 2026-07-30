# Insurance Agent — Component Plan

Part of the Economic Reporting & Policy Coordinator (ERPC). Activated post-event
(after Forecasting Agent issues all-clear). Reads outputs from the Econ and Policy
agents and fills the official USDA CCC-576 (Notice of Loss) PDF form using pypdf.

---

## Status

- [x] Official USDA CCC-576 form filling with pypdf
- [x] Part A header — FSA office, crop year, producer location, FIPS codes, disaster type and dates
- [x] Part A acreage rows — farm ID, planted acres, disaster-affected acres (up to 3 crops)
- [x] Part B production section — crop name, producer share, acreage, salvage value (up to 3 crops)
- [x] Part C inventory losses — crop value before/after disaster, salvage estimate (up to 3 crops)
- [x] Mock fallback — fills form even when econ/status files are missing
- [ ] Real crop agent field data (currently uses `econ_report.json` crop_destructions or mock)
- [ ] Part D forage/grazing section (Items 38–48) — needs Livestock Agent
- [ ] CA DOI standard claim form pre-fill (future — requires stable form field names)
- [ ] Digital signature / farmer review workflow
- [ ] Email/SMS delivery to farmer

---

## File Location

`erpc/insurance_agent.py`

Form template: `forecaster/forms/ccc_576.pdf` (official USDA form, bundled for offline use)

Output: `forecaster/output/ccc_576_filled.pdf`

---

## What This Form Is For

The CCC-576 is the primary USDA Notice of Loss form for ELAP, LFP, LIP, and NAP programs.
It must be filed within 30 days of a loss event at the local FSA office.

A farmer whose land is threatened has hours, not days, to act. After the fire, they
face a second crisis: filing claims across multiple programs (FSA, FEMA, SBA, CA state),
each with different deadlines, different required documents, and different offices.

This fills the real federal form with all available system data so the farmer can
print it, review highlighted blank fields, and walk into the FSA office ready to sign.

Target user: California farmer, post-wildfire event, walking into an FSA office.

---

## Why pypdf, Not reportlab

The CCC-576 has 181 confirmed AcroForm fields with clear tooltips. pypdf
`update_page_form_field_values()` fills them directly:

- Uses the real official government form — legally recognized, correct layout
- No need to reproduce the form's visual design from scratch
- Fields left blank are visually obvious to the farmer
- Works offline — form bundled in repo at `forecaster/forms/ccc_576.pdf`

Official source: https://www.farmers.gov/sites/default/files/documents/ccc-576.pdf

---

## Trigger

Post-event. Called by ERPC after all-clear from Forecasting Agent.
Reads three output files — all must exist or fall back to mock data:

| Input | Path | Fallback |
|-------|------|---------|
| Forecasting Agent output | `forecaster/output/status.json` | `MOCK_STATUS` in agent |
| Econ Agent output | `forecaster/output/econ_report.json` | `MOCK_ECON` in agent |
| Farm config | `forecaster/config/farm_config.json` | Required — no fallback |

---

## CCC-576 Field Coverage

| Section | Items | Fields Pre-filled | Fields Left Blank |
|---------|-------|-------------------|-------------------|
| Part A Header | 1–6 | FSA office, crop year, producer address, FIPS codes, disaster type, disaster dates, first crop name, intended use, date loss first apparent | Crop variety/type, planting period |
| Part A Acreage | 7–8 | Farm ID, intended acres, planted acres, disaster-affected acreage (3 rows) | NAP unit numbers, prevented-planted acres |
| Part B Production | 11–29 | Producer name, crop year, crop name, producer share (100%), acreage, practice code, loss description, salvage value (3 rows) | Unit number, pay codes, stage, actual production, production not to count |
| Part C Inventory | 32–37 | Crop name, producer share, value before disaster, value after disaster (ABANDON=0), salvage estimate (5% of adjusted loss) (3 rows) | Ineligible value (FSA fills) |
| Part D Forage | 38–48 | — | All (livestock agent not yet available) |
| Part E/F Cert. | 49–52 | — | All (FSA officer fills) |
| Signatures | — | — | In-person only |

Total: 56 of 97 mapped fields pre-filled from system data.

---

## Hardcoded Values To Replace

| Value | Currently | Make Dynamic From |
|-------|-----------|-------------------|
| FSA office address | San Diego County FSA Office, 1204 Mission Road, Suite 1, Escondido CA 92029 | USDA FSA office locator API by county |
| State/county FIPS | "06-073" (CA / San Diego) | `farm_config.json` state + county + FIPS lookup table |
| Livestock count (Part D) | Left blank | Livestock Agent inventory |
| Federal crop insurance flag | Left blank | `farmer_profile.json` → `has_federal_crop_insurance` |
| NAP coverage flag | Left blank | `farmer_profile.json` → `has_nap_coverage` |
| Producer legal name | Uses `farm_name` from farm_config.json | `farmer_profile.json` → producer legal name |
| Producer share | "1.000" (100%) | `farmer_profile.json` → ownership share |
| Practice code | "N" (nonirrigated) | Crop Agent `task2` field-level irrigation data |
| Intended use | "Sale" | Crop Agent or farm profile |
| Salvage estimate | 5% of confidence_adjusted_loss_usd | Market data + post-event assessment |

---

## Dependencies & Integration Points

- **Input from Forecasting Agent:** `forecaster/output/status.json` — event date, fire name, threat level
- **Input from Econ Agent:** `forecaster/output/econ_report.json` — `crop_destructions` list (field_id, crop_category, size_acres, estimated_loss_usd, confidence_adjusted_loss_usd, task4_decision)
- **Input from farm_config.json:** farm ID, name, location (county, state, lat/lon)
- **Form template:** `forecaster/forms/ccc_576.pdf` — official USDA form, 181 AcroForm fields, 2 pages
- **Output:** `forecaster/output/ccc_576_filled.pdf`

---

## Future Work

- **Additional FSA forms:** CCC-578 (crop acreage report), CCC-471 (NAP application), AD-2017 (field operations record)
- **CA DOI form fill:** if CA DOI standardizes AcroForm fields, add a pypdf layer
- **Digital delivery:** email or SMS the PDF to the farmer's registered contact
- **Claim status tracking:** post-submission, track which claims have been filed, pending, paid
- **Underpayment flagging:** compare settlement amounts against documented exposure and flag gaps
- **Insurer-specific templates:** large CA ag insurers (Nationwide Agribusiness, Farmers, Zenith) have proprietary claim forms — build templates for each
