"""Policy Agent — part of the Economic Reporting & Policy Coordinator (ERPC).

Activated post-event (after Forecasting Agent issues all-clear).
Evaluates farm eligibility for wildfire aid programs, grants, and recovery
initiatives. Writes output/policy_report.json.

Usage:
    python policy_agent.py [--status path/to/status.json] [--dry-run]

All farm profile fields (insurance status, land types, etc.) are hardcoded
constants in FARM_PROFILE below. Make dynamic by moving to farm_config.json
or farmer_profile.json and injecting at runtime — no changes needed to the
eligibility engine itself.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

from erpc.programs import PROGRAMS, Ctx, resolve
from erpc.common import (
    DATA_DIR, FARM_CONFIG, LIVESTOCK_ERPC_MSG, POLICY_REPORT, REPO_ROOT,
    STATUS_JSON, get_logger, latest_crop_output, load_crop_output, load_json,
    write_report,
)

load_dotenv(REPO_ROOT / ".env")

logger = get_logger("policy_agent")

DEADLINES_CACHE = DATA_DIR / "program_deadlines_cache.json"
DEADLINES_CACHE_MAX_AGE_DAYS = 7

# ---------------------------------------------------------------------------
# Hardcoded farm profile
# All booleans below should eventually come from farm_config.json or
# farmer_profile.json. See POLICY_AGENT_PLAN.md — "Farm Profile Fields" table.
# ---------------------------------------------------------------------------

FARM_PROFILE = {
    "has_livestock": True,            # → ELRP, ELAP, LFP, LIP
    "has_crops": True,                # → NAP, SDRP
    "has_federal_crop_insurance": False,  # NAP hard exclusion if True
    "has_nap_coverage": True,         # NAP requires pre-event coverage election
    "has_forested_parcels": False,    # → EFRP, EQIP_FIRE (forested variant)
    "land_types": ["cropland", "rangeland"],  # → EQIP, EWP
    "underserved_producer": False,    # ECP cost-share: 90% if True, 75% if False
    "country": "US",                  # FAO/GCF gate: only non-US farms qualify
    "has_approved_lfp": False,        # ELRP gateway: auto-payment requires approved LFP
}

# Hardcoded loss summary — replace with real data from Crop + Livestock agents.
# See POLICY_AGENT_PLAN.md — "Loss Summary Input" section.
HARDCODED_LOSS_SUMMARY = {
    "crop_loss": True,
    "livestock_loss": True,
    "livestock_deaths": True,
    "forage_loss": True,
    "infrastructure_damage": True,
    "forested_parcel_damage": False,
    "watershed_damage": False,
    "economic_injury": True,
}

# Program query templates for Tavily search
_PROGRAM_QUERY_TEMPLATES = {
    "ELRP_2025": "ELRP {year} Emergency Livestock Relief Program wildfire eligibility requirements",
    "ELAP":      "ELAP {year} Emergency Assistance Livestock wildfire {state} eligibility deadline",
    "LFP":       "LFP {year} Livestock Forage Disaster Program wildfire eligibility payment rates",
    "LIP":       "LIP {year} Livestock Indemnity Program wildfire {state} acceptance rate",
    "NAP":       "NAP {year} Noninsured Crop Disaster Assistance wildfire eligibility California",
    "ECP":       "ECP {year} Emergency Conservation Program wildfire eligibility cost-share",
    "FSA_LOAN":  "FSA Emergency Farm Loan {year} wildfire disaster eligibility requirements",
    "EQIP_FIRE": "EQIP {year} wildfire conservation practice eligibility {state}",
    "EWP":       "NRCS EWP Emergency Watershed Protection {year} wildfire eligibility",
    "FEMA_IA":   "FEMA Individual Assistance {year} wildfire California eligibility",
    "FEMA_HMGP": "FEMA HMGP {year} wildfire hazard mitigation grant eligibility",
    "SBA_EIDL":  "SBA EIDL {year} wildfire disaster loan agricultural eligibility",
    "CDFA_ERL":  "CDFA Emergency Relief Program {year} California wildfire farm eligibility",
}

_PROGRAM_LOSS_THRESHOLDS = {
    "ELRP_2025": ("livestock_value_at_risk_usd", 1000),
    "ELAP":      ("livestock_value_at_risk_usd", 500),
    "LFP":       ("livestock_value_at_risk_usd", 1000),
    "LIP":       ("livestock_potential_loss_usd", 1),
    "NAP":       ("crop_loss_usd", 5000),
    "ECP":       ("crop_loss_usd", 1000),
    "FSA_LOAN":  ("crop_loss_usd", 10000),
    "SBA_EIDL":  ("crop_loss_usd", 5000),
}

# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

@dataclass
class EligibleProgram:
    program_id: str
    name: str
    agency: str
    category: str           # livestock | crop | conservation | loan | mitigation | international | state
    eligibility_status: str # confirmed | likely | check_required | ineligible
    eligibility_reason: str
    deadline: Optional[str]
    deadline_trigger: Optional[str]
    estimated_value: Optional[str]
    required_docs: list[str]
    link: str
    notes: Optional[str]
    requires_disaster_declaration: bool
    declaration_confirmed: Optional[bool]
    acceptance_chance: Optional[int] = None
    web_sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Status ordering for sort
# ---------------------------------------------------------------------------

_STATUS_ORDER = {"confirmed": 0, "likely": 1, "check_required": 2, "ineligible": 3}


# ---------------------------------------------------------------------------
# FEMA declaration check
# ---------------------------------------------------------------------------

def _check_fema_declaration(state: str, county: str) -> Optional[bool]:
    """Query OpenFEMA for an active Fire declaration in this county.

    Returns True if found, False if not found, None if API call fails.
    Cache TTL is handled by the caller; this always makes a live request.
    """
    url = "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries"
    params = {
        "$filter": f"incidentType eq 'Fire' and state eq '{state}' and designatedArea eq '{county} (County)'",
        "$orderby": "declarationDate desc",
        "$top": 1,
        "$format": "json",
    }
    try:
        resp = httpx.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        found = len(data.get("DisasterDeclarationsSummaries", [])) > 0
        logger.info("FEMA declaration check: %s County, %s → %s", county, state, "FOUND" if found else "NOT FOUND")
        return found
    except Exception as exc:
        logger.warning("FEMA declaration API failed: %s — treating as unknown", exc)
        return None


# ---------------------------------------------------------------------------
# Loss summary from JSONs (Tavily enrichment)
# ---------------------------------------------------------------------------

def _derive_loss_from_jsons() -> dict:
    """Read crop + livestock JSONs. Returns loss_summary with _numeric passthrough."""
    loss = dict(HARDCODED_LOSS_SUMMARY)
    numeric = {
        "crop_loss_usd": 0.0,
        "crop_confidence_adjusted_loss_usd": 0.0,
        "livestock_value_at_risk_usd": 0.0,
        "livestock_total_animals": 0,
        "livestock_potential_loss_usd": 0.0,
        "transport_costs_usd": 0.0,
    }

    lv = load_json(LIVESTOCK_ERPC_MSG)
    if lv:
        opt = lv.get("cost_optimization", {})
        numeric["livestock_value_at_risk_usd"] = lv.get("animal_valuation_at_risk", 0)
        numeric["livestock_total_animals"] = opt.get("total_animals_at_risk", 0)
        numeric["livestock_potential_loss_usd"] = opt.get("potential_loss_usd", 0)
        numeric["transport_costs_usd"] = lv.get("transport_costs_usd", 0)
        loss["livestock_loss"] = numeric["livestock_value_at_risk_usd"] > 0
        loss["livestock_deaths"] = numeric["livestock_potential_loss_usd"] > 0
        logger.info("Derived livestock loss: at_risk=$%s", numeric["livestock_value_at_risk_usd"])
    else:
        logger.warning("Could not read %s", LIVESTOCK_ERPC_MSG.name)

    crop, crop_path = load_crop_output()
    if crop_path:
        ei = crop["task2"]
        numeric["crop_loss_usd"] = ei.get("total_estimated_loss_usd", 0)
        numeric["crop_confidence_adjusted_loss_usd"] = ei.get("total_confidence_adjusted_loss_usd", 0)
        loss["crop_loss"] = numeric["crop_loss_usd"] > 0
        abandoned = any(d.get("decision") == "ABANDON" for d in crop["task4"])
        loss["infrastructure_damage"] = abandoned or HARDCODED_LOSS_SUMMARY["infrastructure_damage"]
        logger.info("Derived crop loss from %s: total=$%s", crop_path.name, numeric["crop_loss_usd"])
    else:
        logger.warning("No crop agent output found — crop loss treated as unknown")

    loss["economic_injury"] = numeric["crop_loss_usd"] > 0 or numeric["livestock_value_at_risk_usd"] > 0
    loss["_numeric"] = numeric
    return loss


# ---------------------------------------------------------------------------
# Scoring helpers for Tavily enrichment
# ---------------------------------------------------------------------------

def _score_loss_match(program_id: str, numeric: dict) -> int:
    """Return 0-30 pts based on loss amounts vs. program thresholds."""
    entry = _PROGRAM_LOSS_THRESHOLDS.get(program_id)
    if not entry:
        return 15
    field, threshold = entry
    amount = numeric.get(field, 0)
    if amount <= 0:
        return 0
    if amount >= threshold * 10:
        return 30
    if amount >= threshold:
        return 20
    return 5


def _score_web_signals(snippets: str) -> int:
    """Scan snippets for acceptance rates/approval signals. Return 0-20 pts."""
    pct_pattern = re.compile(r'(\d{1,3})\s*%\s*(?:approval|acceptance|funded|approved)', re.I)
    near_pattern = re.compile(r'(?:approval|acceptance)\s+rate[^.]{0,50}?(\d{1,3})\s*%', re.I)
    matches = pct_pattern.findall(snippets) + near_pattern.findall(snippets)
    if matches:
        try:
            rate = max(int(m) for m in matches)
            return min(20, int(rate * 0.2))
        except ValueError:
            pass
    positive = sum(1 for kw in ["approved", "eligible", "qualify", "available", "open"]
                   if kw in snippets)
    negative = sum(1 for kw in ["closed", "expired", "ineligible", "no longer", "ended"]
                   if kw in snippets)
    net = positive - negative
    if net >= 3:
        return 15
    if net >= 1:
        return 10
    if net <= -1:
        return 2
    return 8


def _score_profile_match(program: EligibleProgram, numeric: dict) -> int:
    """Return 0 or 10 pts based on farm profile match."""
    if program.category == "livestock":
        return 10 if numeric.get("livestock_value_at_risk_usd", 0) > 0 else 0
    if program.category == "crop":
        return 10 if numeric.get("crop_loss_usd", 0) > 0 else 0
    return 10 if program.category in ("conservation", "loan", "mitigation", "state") else 5


def _maybe_update_deadline_from_web(program: EligibleProgram, snippets: str) -> None:
    """Opportunistically update vague deadlines from Tavily text."""
    if program.deadline and re.match(r'\d{4}-\d{2}-\d{2}', str(program.deadline)):
        return
    date_pattern = re.compile(
        r'(?:deadline|apply by|due by|by)\s*:?\s*'
        r'((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+20\d\d)',
        re.I
    )
    match = date_pattern.search(snippets)
    if match:
        program.deadline = f"Web-sourced: {match.group(1).strip()}"
        logger.info("Updated deadline for %s: %s", program.program_id, program.deadline)


# ---------------------------------------------------------------------------
# Grants.gov live query
# ---------------------------------------------------------------------------

def _fetch_grants_gov(keywords: list[str]) -> list[EligibleProgram]:
    """Query Grants.gov for open opportunities matching wildfire + agriculture."""
    url = "https://api.grants.gov/v1/api/search2"
    results = []

    for keyword in keywords:
        payload = {
            "keyword": keyword,
            "oppStatuses": "forecasted|posted",
            "rows": 10,
            "sortBy": "openDate|desc",
        }
        try:
            resp = httpx.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            hits = data.get("data", {}).get("oppHits", [])
            for hit in hits:
                program_id = f"GRANTSGOV_{hit.get('id', 'UNKNOWN')}"
                if any(p.program_id == program_id for p in results):
                    continue
                results.append(EligibleProgram(
                    program_id=program_id,
                    name=hit.get("title", "Unknown Grant"),
                    agency=hit.get("agencyName", "Federal Agency"),
                    category="grant",
                    eligibility_status="check_required",
                    eligibility_reason="Live grant from Grants.gov — verify eligibility directly",
                    deadline=hit.get("closeDate"),
                    deadline_trigger=None,
                    estimated_value=f"${hit['awardCeiling']:,}" if hit.get("awardCeiling") else None,
                    required_docs=[],
                    link=f"https://www.grants.gov/search-results-detail/{hit.get('id', '')}",
                    notes=f"Keyword match: '{keyword}'",
                    requires_disaster_declaration=False,
                    declaration_confirmed=None,
                ))
            logger.info("Grants.gov '%s': %d results", keyword, len(hits))
        except Exception as exc:
            logger.warning("Grants.gov query failed for '%s': %s", keyword, exc)

    return results


# ---------------------------------------------------------------------------
# Tavily search enrichment
# ---------------------------------------------------------------------------

def _enrich_with_tavily(programs: list[EligibleProgram], farm_context: dict) -> list[EligibleProgram]:
    """Enrich each program with acceptance_chance and web_sources via Tavily."""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        logger.warning("TAVILY_API_KEY not set — skipping Tavily enrichment")
        return programs

    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=api_key)
    except ImportError:
        logger.warning("tavily-python not installed — skipping Tavily enrichment")
        return programs

    state = farm_context.get("state", "CA")
    year = datetime.now().year
    numeric = farm_context.get("loss", {}).get("_numeric", {})

    for program in programs:
        # Ineligible: score 0, no API call
        if program.eligibility_status == "ineligible":
            program.acceptance_chance = 0
            continue

        # Build targeted query
        template = _PROGRAM_QUERY_TEMPLATES.get(program.program_id)
        if template:
            query = template.format(year=year, state=state)
        else:
            query = f"{program.name} {year} wildfire eligibility {state}"

        # Call Tavily
        try:
            response = client.search(
                query=query,
                search_depth="basic",
                max_results=5,
                include_answer=True,
                include_raw_content=False,
            )
            results = response.get("results", [])
            answer = response.get("answer", "") or ""
            sources = [r["url"] for r in results if r.get("url")]
            snippets = " ".join(
                r.get("content", "") for r in results
            ).lower() + answer.lower()

            program.web_sources = sources[:5]

        except Exception as exc:
            logger.warning("Tavily search failed for %s: %s", program.program_id, exc)
            continue

        # Score components
        status_score = {"confirmed": 40, "likely": 30, "check_required": 15}.get(
            program.eligibility_status, 0
        )
        loss_score = _score_loss_match(program.program_id, numeric)
        web_score = _score_web_signals(snippets)
        profile_score = _score_profile_match(program, numeric)

        program.acceptance_chance = min(100, status_score + loss_score + web_score + profile_score)

        # Opportunistically update deadline
        _maybe_update_deadline_from_web(program, snippets)

    return programs


# ---------------------------------------------------------------------------
# Deadlines cache
# ---------------------------------------------------------------------------

def _load_deadlines_cache() -> dict:
    """Read program_deadlines_cache.json. Returns empty dict if missing or stale."""
    if not DEADLINES_CACHE.exists():
        logger.warning("Deadlines cache not found at %s — using hardcoded deadlines", DEADLINES_CACHE)
        return {}
    try:
        with open(DEADLINES_CACHE) as f:
            cache = json.load(f)
        written_at = datetime.fromisoformat(cache.get("written_at", "2000-01-01"))
        age_days = (datetime.now(timezone.utc) - written_at.replace(tzinfo=timezone.utc)).days
        if age_days > DEADLINES_CACHE_MAX_AGE_DAYS:
            logger.warning("Deadlines cache is %d days old (max %d) — using hardcoded deadlines", age_days, DEADLINES_CACHE_MAX_AGE_DAYS)
            return {}
        logger.info("Loaded deadlines cache (age: %d days)", age_days)
        return cache.get("programs", {})
    except Exception as exc:
        logger.warning("Failed to read deadlines cache: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Disaster event date
# ---------------------------------------------------------------------------

def _load_event_date(status_path: Path) -> Optional[datetime]:
    """Extract fire event date from status.json.

    Prefers nearest_fire.detected_at; falls back to status timestamp.
    Make dynamic: replace with official FEMA declaration date once integrated.
    """
    status = load_json(status_path)
    detected = (status.get("nearest_fire") or {}).get("detected_at") or status.get("timestamp")
    if not detected:
        logger.warning("No event date in %s", status_path.name)
        return None
    try:
        return datetime.fromisoformat(detected.replace("Z", "+00:00"))
    except ValueError as exc:
        logger.warning("Unparseable event date %r: %s", detected, exc)
        return None


# ---------------------------------------------------------------------------
# Eligibility engine
#
# The program catalog itself lives in erpc/programs.py as declarative records.
# This loop is the only thing that turns those records into report entries, so
# adding a program never means touching code here.
# ---------------------------------------------------------------------------

def _build_catalog(
    farm_config: dict,
    declaration: Optional[bool],
    event_date: Optional[datetime],
    deadlines_cache: dict,
    loss: dict,
) -> list[EligibleProgram]:
    """Evaluate every catalog program against this farm."""
    ctx = Ctx(
        profile=FARM_PROFILE,
        loss=loss,
        state=farm_config["location"]["state"],
        declaration=declaration,
        event_date=event_date,
        deadlines_cache=deadlines_cache,
    )

    catalog: list[EligibleProgram] = []
    for spec in PROGRAMS:
        if spec.states and ctx.state not in spec.states:
            continue
        verdict = spec.evaluate(ctx)
        if verdict is None:
            continue  # not applicable to this farm at all — leave it out
        status, reason = verdict
        blank = spec.blank_when_ineligible and status == "ineligible"

        catalog.append(EligibleProgram(
            program_id=spec.program_id,
            name=spec.name,
            agency=spec.agency,
            category=spec.category,
            eligibility_status=status,
            eligibility_reason=reason,
            deadline=None if blank else resolve(spec.deadline, ctx),
            deadline_trigger=None if blank else spec.deadline_trigger,
            estimated_value=None if blank else resolve(spec.estimated_value, ctx),
            required_docs=[] if blank else list(spec.required_docs),
            link=spec.link,
            notes=None if blank else resolve(spec.notes, ctx),
            requires_disaster_declaration=spec.requires_disaster_declaration,
            declaration_confirmed=declaration if spec.report_declaration else None,
        ))
    return catalog


# ---------------------------------------------------------------------------
# Main agent class
# ---------------------------------------------------------------------------

class PolicyAgent:
    def __init__(self, farm_config_path: str | Path = FARM_CONFIG,
                 status_path: str | Path = STATUS_JSON):
        self.farm_config = load_json(Path(farm_config_path))
        self.status_path = Path(status_path)
        self.declaration: Optional[bool] = None
        self.event_date: Optional[datetime] = None
        self.programs: list[EligibleProgram] = []
        self.report: dict = {}

    def run(self, loss_summary: Optional[dict] = None, dry_run: bool = False) -> dict:
        """Run the full policy eligibility pipeline. Returns the report dict.

        `dry_run` skips every outbound API call (FEMA, Grants.gov, Tavily) and
        evaluates the catalog against HARDCODED_LOSS_SUMMARY.
        """
        if loss_summary is not None:
            loss = loss_summary
        elif dry_run:
            loss = dict(HARDCODED_LOSS_SUMMARY)
        else:
            loss = _derive_loss_from_jsons()

        state = self.farm_config["location"]["state"]
        county = self.farm_config["location"]["county"]

        self.event_date = _load_event_date(self.status_path)
        logger.info("Event date: %s", self.event_date)

        self.declaration = None if dry_run else _check_fema_declaration(state, county)

        self.programs = _build_catalog(
            farm_config=self.farm_config,
            declaration=self.declaration,
            event_date=self.event_date,
            deadlines_cache=_load_deadlines_cache(),
            loss=loss,
        )

        if dry_run:
            logger.info("Dry run — skipping Grants.gov and Tavily calls")
        else:
            self.programs.extend(_fetch_grants_gov(["wildfire agriculture", "wildfire livestock"]))
            self.programs = _enrich_with_tavily(
                self.programs, {"state": state, "county": county, "loss": loss}
            )

        # Sort by eligibility status, then by acceptance chance descending
        self.programs.sort(
            key=lambda p: (_STATUS_ORDER.get(p.eligibility_status, 99), -(p.acceptance_chance or 0))
        )

        by_status = {
            s: sum(1 for p in self.programs if p.eligibility_status == s)
            for s in ("confirmed", "likely", "check_required", "ineligible")
        }
        tavily_enriched = sum(1 for p in self.programs if p.acceptance_chance is not None)
        crop_path = latest_crop_output()

        self.report = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "farm_id": self.farm_config["farm_id"],
            "state": state,
            "county": county,
            "disaster_declaration_confirmed": self.declaration,
            "event_date": self.event_date.isoformat() if self.event_date else None,
            "tavily_enrichment": {
                "enabled": not dry_run,
                "programs_scored": tavily_enriched,
                "programs_skipped": len(self.programs) - tavily_enriched,
            },
            "data_sources": {
                "loss_summary": "hardcoded" if dry_run else "live_jsons",
                "crop_json": str(crop_path) if crop_path else None,
                "livestock_json": str(LIVESTOCK_ERPC_MSG) if LIVESTOCK_ERPC_MSG.exists() else None,
            },
            "summary": {"total_programs_evaluated": len(self.programs), **by_status},
            "eligible_programs": [p.to_dict() for p in self.programs
                                  if p.eligibility_status != "ineligible"],
            "ineligible_programs": [p.to_dict() for p in self.programs
                                    if p.eligibility_status == "ineligible"],
        }

        write_report(POLICY_REPORT, self.report)
        return self.report

    def print_summary(self) -> None:
        s = self.report.get("summary", {})
        print("\n--- POLICY REPORT SUMMARY ---")
        print(f"  Declaration confirmed : {self.report.get('disaster_declaration_confirmed')}")
        print(f"  Event date            : {self.report.get('event_date')}")
        print(f"  Confirmed             : {s.get('confirmed')}")
        print(f"  Likely                : {s.get('likely')}")
        print(f"  Check required        : {s.get('check_required')}")
        print(f"  Ineligible            : {s.get('ineligible')}")
        print("\n  Eligible programs:")
        for p in self.report.get("eligible_programs", []):
            deadline_str = f" | deadline: {p['deadline']}" if p.get("deadline") else ""
            print(f"  [{p['eligibility_status'].upper():14}] {p['name']}{deadline_str}")
            if p.get("notes"):
                print(f"                   ^ {p['notes']}")
        print("\n  output/policy_report.json written.")


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Policy Agent — wildfire aid eligibility engine")
    parser.add_argument("--status", default=str(STATUS_JSON), help="Path to forecaster status.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip FEMA/Grants.gov/Tavily calls and use the hardcoded loss summary")
    args = parser.parse_args()

    agent = PolicyAgent(status_path=args.status)
    agent.run(dry_run=args.dry_run)
    agent.print_summary()


if __name__ == "__main__":
    main()
