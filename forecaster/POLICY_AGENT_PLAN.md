# Policy Agent — Component Plan

Part of the Economic Reporting & Policy Coordinator (ERPC). Activated post-event
(all-clear signal from Forecasting Agent). Returns a structured eligibility list of
aid programs, grants, subsidies, and recovery initiatives the farmer qualifies for.

---

## Status

- [x] Hardcoded program catalog (21 programs — 9 FSA, 2 NRCS, 3 FEMA, 1 SBA, 2 UN/FAO, 4 CA state)
- [x] Eligibility engine (rule evaluation against farm profile)
- [x] FEMA Disaster Declarations API integration
- [x] Grants.gov API integration
- [x] Program deadlines cache (weekly scrape of farmers.gov)
- [x] Structured output format finalized
- [x] Loss summary derived from `econ_report.json` (crop_loss, livestock_loss, economic_injury flags)
- [ ] Integration with ERPC pipeline trigger
- [ ] `farmer_profile.json` for remaining hardcoded eligibility flags

---

## File Location

`erpc/policy_agent.py`

Output written to: `forecaster/output/policy_report.json`

---

## Architecture

### Trigger
Post-event only. Called by ERPC when Forecasting Agent issues all-clear.
Input: farm profile + loss summary from Crop/Livestock agents.

### Loss Summary Input

The policy agent derives loss flags from `forecaster/output/econ_report.json` at runtime.
Three flags are now live; the rest remain hardcoded as conservative defaults.

| Flag | Source | Status |
|------|--------|--------|
| `crop_loss` | `econ_report.json` → `crop_loss_total_usd > 0` | Live |
| `livestock_loss` | `econ_report.json` → `livestock_at_risk_usd > 0` | Live |
| `economic_injury` | `econ_report.json` → `total_exposure_usd > 0` | Live |
| `livestock_deaths` | `HARDCODED_LOSS_SUMMARY` | Hardcoded (no source yet) |
| `forage_loss` | `HARDCODED_LOSS_SUMMARY` | Hardcoded (no source yet) |
| `infrastructure_damage` | `HARDCODED_LOSS_SUMMARY` | Hardcoded (no source yet) |
| `forested_parcel_damage` | `HARDCODED_LOSS_SUMMARY` | Hardcoded False |
| `watershed_damage` | `HARDCODED_LOSS_SUMMARY` | Hardcoded False |

Falls back to full `HARDCODED_LOSS_SUMMARY` if `econ_report.json` is unavailable.

### Disaster Event Date

Sourced from `forecaster/output/status.json` → `nearest_fire.detected_at`.
This is the fire's first detection timestamp, used to compute window-based deadlines
(e.g., "30 days from Notice of Loss", "8 months from declaration date").
Falls back to `status.json` → `timestamp` (forecaster run time) if `detected_at` is absent.
Make dynamic: replace with official FEMA declaration date once that API is integrated.

### Pipeline

1. **FEMA Declaration Check** (most important step)
   - Query OpenFEMA Disaster Declarations API filtered by `incidentType=Fire` and farmer's county + state
   - Result gates which USDA and FEMA programs are even available
   - Cache TTL: 6 hours in Stage 2, re-checked each Stage 1 cycle
   - Endpoint: `https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries`
   - Filter params: `?$filter=incidentType eq 'Fire' and state eq '{state}' and designatedArea eq '{county}'`

2. **Eligibility Engine**
   - Runs each program's rules against the farm profile
   - Returns: `confirmed` / `likely` / `check_required` / `ineligible`
   - Hard exclusions evaluated first (e.g., NAP blocked if farmer has federal crop insurance)

3. **Grants.gov Live Query**
   - Keyword search: `"wildfire agriculture"` and `"wildfire livestock"`
   - Free REST, no key required
   - Supplements hardcoded catalog with any newly opened grants
   - Endpoint: `https://api.grants.gov/v1/api/search2`

4. **Deadlines Cache Read**
   - Reads `data/program_deadlines_cache.json`
   - Written by a separate weekly scraper (not this agent) from `https://www.farmers.gov/working-with-us/program-deadlines`
   - If cache age > 7 days: log warning, proceed with stale data (never block on this)

5. **Output Assembly**
   - Sorted by: eligibility confidence (confirmed first), then by estimated value (descending)
   - Each item: program name, agency, eligibility status, deadline, required docs, estimated amount, link

---

## Output Schema (per program)

```python
{
    "program_id": str,           # e.g. "ELRP_2025"
    "name": str,
    "agency": str,               # USDA-FSA, USDA-NRCS, FEMA, SBA, UN/FAO
    "category": str,             # livestock | crop | conservation | loan | mitigation | international
    "eligibility_status": str,   # confirmed | likely | check_required | ineligible
    "eligibility_reason": str,   # human-readable explanation
    "deadline": str | None,      # ISO date or plain string if no fixed date
    "deadline_trigger": str | None,  # e.g. "30 days from Notice of Loss"
    "estimated_value": str | None,   # e.g. "$500,000 max" — string, not parsed
    "required_docs": list[str],
    "link": str,
    "notes": str | None,         # e.g. "LFP application required first (gateway dependency)"
    "requires_disaster_declaration": bool,
    "declaration_confirmed": bool | None,  # None if not yet checked
}
```

---

## Hardcoded Program Catalog

All 21 programs below are hardcoded pending dynamic data sources.
When a field is made dynamic, mark it with the source.

### USDA — Farm Service Agency (FSA)

| ID | Program | Key Eligibility Rules (Hardcoded) | Make Dynamic From |
|----|---------|-----------------------------------|-------------------|
| ELRP_2025 | Emergency Livestock Relief Program | Has livestock; has approved LFP on file; wildfire on non-federally managed land; enrollment Sep 15 – Nov 21 2025 | Farmers.gov deadlines cache |
| ELAP | Emergency Assistance for Livestock, Honeybees & Farm-Raised Fish | Has livestock, honeybees, or farm-raised fish; Notice of Loss filed within 30 days of event | FSA Program Handbooks |
| LFP | Livestock Forage Disaster Program | Has livestock; forage loss from wildfire; also gateway to ELRP — flag dependency | FSA Program Handbooks |
| LIP | Livestock Indemnity Program | Livestock deaths above normal mortality from wildfire; Notice of Loss by Mar 1 2027 for 2026 losses | FSA Program Handbooks |
| NAP | Noninsured Crop Disaster Assistance Program | Does NOT have federal crop insurance (hard exclusion); elected NAP coverage before disaster event | FSA Program Handbooks |
| ECP | Emergency Conservation Program | Farmland damaged by wildfire; fencing/water/debris; 90% cost-share if underserved, 75% otherwise | Farm profile: underserved flag |
| EFRP | Emergency Forest Restoration Program | Has non-industrial private forested parcels damaged by wildfire | Farm profile: land type |
| SDRP_2324 | Supplemental Disaster Relief Program 2023/2024 | Crop revenue losses in 2023 or 2024 weather events | Static (program-specific years) |
| FSA_LOAN | FSA Emergency Farm Loans | Farm/ranch/aquaculture; production or property loss; apply within 8 months of disaster declaration | FEMA declaration date |

### USDA — Natural Resources Conservation Service (NRCS)

| ID | Program | Key Eligibility Rules (Hardcoded) | Make Dynamic From |
|----|---------|-----------------------------------|-------------------|
| EQIP_FIRE | Environmental Quality Incentives Program — Wildfire | Cropland, rangeland, or non-industrial private forestland impacted by wildfire | Farm profile: land type |
| EWP | Emergency Watershed Protection Program | Submit formal request to state conservationist within 60 days of disaster | FEMA declaration date |

### FEMA

| ID | Program | Key Eligibility Rules (Hardcoded) | Make Dynamic From |
|----|---------|-----------------------------------|-------------------|
| FEMA_IA | Individual Assistance | Presidential Major Disaster Declaration required; farmer qualifies as individual/household | FEMA Declarations API |
| FEMA_FMAG | Fire Management Assistance Grant | Not direct to farmers; activates other programs; requires state-level FMAG declaration | FEMA Declarations API |
| FEMA_HMGP | Hazard Mitigation Grant Program | Available up to 12 months after presidentially-declared major disaster; long-term mitigation projects | FEMA declaration date |

### SBA

| ID | Program | Key Eligibility Rules (Hardcoded) | Make Dynamic From |
|----|---------|-----------------------------------|-------------------|
| SBA_EIDL | Economic Injury Disaster Loans | Small business or small agricultural cooperative; up to $2M; cash flow losses | SBA disaster declaration list |

### UN / International

| ID | Program | Key Eligibility Rules (Hardcoded) | Make Dynamic From |
|----|---------|-----------------------------------|-------------------|
| FAO_FIRE_HUB | FAO Global Fire Management Hub | International farmers only (non-US); policy/coordination body, not direct aid | Farm profile: country |
| GCF_FAO | Green Climate Fund via FAO | Access through national government applications only; not direct farmer enrollment | Static (structural constraint) |

---

## Hardcoded Values To Replace

| Value | Current | Target Source |
|-------|---------|---------------|
| ELRP enrollment window | Sep 15 – Nov 21, 2025 | `program_deadlines_cache.json` (weekly scrape) |
| LIP Notice of Loss deadline | Mar 1, 2027 for 2026 losses | `program_deadlines_cache.json` |
| EWP request window | 60 days from disaster | FEMA declaration date (computed) |
| FSA Emergency Loan window | 8 months from declaration | FEMA declaration date (computed) |
| HMGP availability window | 12 months from declaration | FEMA declaration date (computed) |
| ECP cost-share rate | 90% underserved / 75% other | Farm profile: `underserved_producer` flag |
| SBA EIDL max | $2M | SBA announcement feed (low priority) |
| FSA Emergency Loan max | $500K | FSA announcement feed (low priority) |
| NAP exclusion rule | No federal crop insurance | Farm profile: `has_federal_crop_insurance` flag |
| FEMA IA gate | Presidential declaration | OpenFEMA API |
| FMAG gate | State-level FMAG declaration | OpenFEMA API |
| SDRP years | 2023/2024 only | Static (program is year-bound) |

---

## Farm Profile Fields Required (Currently Hardcoded in Agent)

These fields drive eligibility decisions. They are hardcoded as constants inside
`policy_agent.py`. Future work: move all of these into `farm_config.json` (or a
separate `farmer_profile.json`) and inject at runtime — no code changes needed in
the eligibility engine itself, just the data source.

`state` and `county` are now in `farm_config.json` → `location` and read from there.
All others remain hardcoded constants in the agent.

| Field | Type | Hardcoded Value | Drives | Make Dynamic From |
|-------|------|-----------------|--------|-------------------|
| `has_livestock` | bool | `True` | ELRP, ELAP, LFP, LIP | `farm_config.json` (derivable from zone `animals` count > 0) |
| `has_crops` | bool | `True` | NAP, SDRP | `farm_config.json` (derivable from zone `crops` list) |
| `has_federal_crop_insurance` | bool | `False` | NAP hard exclusion | `farmer_profile.json` |
| `has_nap_coverage` | bool | `True` | NAP (required pre-event) | `farmer_profile.json` |
| `has_forested_parcels` | bool | `False` | EFRP, EQIP | `farm_config.json` land type |
| `land_types` | list[str] | `["cropland", "rangeland"]` | EQIP, EWP | `farm_config.json` |
| `underserved_producer` | bool | `False` | ECP cost-share rate (75% vs 90%) | `farmer_profile.json` |
| `country` | str | `"US"` | FAO/GCF eligibility gate | `farmer_profile.json` |
| `has_approved_lfp` | bool | `False` | ELRP gateway dependency flag | `farmer_profile.json` |
| `state` | str | read from `farm_config.json` | FEMA declaration query | Already dynamic |
| `county` | str | read from `farm_config.json` | FEMA declaration query | Already dynamic |

---

## Data Sources

| Source | Used For | Auth | Caching |
|--------|---------|------|---------|
| OpenFEMA Disaster Declarations | Gates FEMA/USDA program availability | None | 6h TTL |
| OpenFEMA IA Housing | Confirms IA availability by county | None | 6h TTL |
| Grants.gov Search API | Live supplemental grants | None | 24h TTL |
| SAM.gov Assistance Listings | Full CFDA program eligibility rules | API key required | SKIPPED — out of scope |
| USDA FSA Program Handbooks | Detailed rule ingestion (RAG later) | None (PDF) | Static |
| Farmers.gov Program Deadlines | Live enrollment windows | None (scrape) | Weekly, separate job |

---

## Dependencies & Integration Points

- **Input from Forecasting Agent:** `forecaster/output/status.json` → `threat_level`, `nearest_fire.detected_at`
- **Input from Econ Agent:** `forecaster/output/econ_report.json` → derives `crop_loss`, `livestock_loss`, `economic_injury` flags; remaining loss flags fall back to `HARDCODED_LOSS_SUMMARY`
- **Output written to:** `forecaster/output/policy_report.json`
- **Output to ERPC:** structured list of `EligibleProgram` objects (schema above)
- **Gateway dependency to flag:** LFP must be filed before ELRP auto-payment triggers — policy agent must surface this ordering in output

---

## Decisions

| Decision | Choice | Notes |
|----------|--------|-------|
| Farm profile location | Hardcoded in policy agent | All eligibility fields listed in "Farm Profile Fields Required" table are hardcoded constants inside the agent. Future work: move to `farm_config.json` or a separate `farmer_profile.json` and inject at runtime. |
| SAM.gov | Skipped | Out of scope. Not included in data sources or eligibility engine. |
| Deadline scraper | `forecaster/scripts/scrape_deadlines.py` | Separate script, run weekly as cron job, writes to `forecaster/data/program_deadlines_cache.json`. Policy agent reads from cache; never calls farmers.gov inline. |
| Geographic scope | California only | State-level programs cover CA programs only: CalFire grants and CA Dept of Food & Agriculture disaster programs. Other states out of scope for now. |

---

## California State Programs (CA-only scope)

These supplement the federal catalog above. Eligibility gated on `state == "CA"`.

| ID | Program | Agency | Key Eligibility Rules (Hardcoded) | Link |
|----|---------|--------|-----------------------------------|------|
| CDFA_ERL | CA Dept of Food & Agriculture — Emergency Relief Programs | CDFA | CA farm or ranch; wildfire loss; governor's emergency declaration required | https://www.cdfa.ca.gov/grants/ |
| CALFIRE_FRAP | CAL FIRE — Forest Health Grants | CAL FIRE | Non-industrial private forest landowner in CA; reforestation or fire resilience work | https://www.fire.ca.gov/grants |
| CDFA_OEFI | CA Office of Emergency Food and Farming Infrastructure | CDFA | Small/mid-scale CA farm; food system disruption from disaster | https://www.cdfa.ca.gov/oefi/ |
| CA_EDD_DISASTER | CA EDD Disaster Unemployment Assistance | CA EDD | Farm workers or self-employed farmers who lost work due to disaster declaration | https://edd.ca.gov/en/unemployment/disaster/ |

> Note: CA state program eligibility rules are less machine-readable than federal programs.
> Deadlines and award amounts for CA programs are highly variable — treat all as `check_required`
> until a CA-specific deadline scraper is added.
