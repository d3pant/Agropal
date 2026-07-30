"""Declarative catalog of wildfire aid programs.

Each program is one `ProgramSpec` entry in `PROGRAMS`: static metadata (name,
agency, link, docs) sits in the record, and the parts that depend on the farm —
eligibility status, reason, deadline — are small callables evaluated against a
`Ctx`. Adding a program means appending one record here; the engine that reads
these records (`_build_catalog` in policy_agent.py) never changes.

An `evaluate` returning None omits the program from the report entirely. Return
an ("ineligible", reason) pair instead when the farmer should still see it, so
they know it was considered and why it did not apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Sequence, Union

# A field that may be a plain value or a function of the evaluation context.
Dynamic = Union[str, None, Callable[["Ctx"], Optional[str]]]
# (status, reason), or None to leave the program out of the report.
Verdict = Optional[tuple[str, str]]


@dataclass(frozen=True)
class Ctx:
    """Everything a program needs to judge one farm."""

    profile: dict
    loss: dict
    state: str
    declaration: Optional[bool]
    event_date: Optional[datetime]
    deadlines_cache: dict

    @property
    def ecp_rate(self) -> str:
        return "90%" if self.profile["underserved_producer"] else "75%"

    def decl_note(self) -> str:
        if self.declaration is True:
            return "Presidential disaster declaration confirmed for this county"
        if self.declaration is False:
            return ("No Presidential disaster declaration found for this county — "
                    "verify at disasterassistance.gov")
        return "Could not verify disaster declaration — check manually"

    def decl_status(self, fallback: str = "likely") -> str:
        if self.declaration is True:
            return "confirmed"
        if self.declaration is False:
            return "check_required"
        return fallback

    def cached_deadline(self, program_id: str, default: Optional[str]) -> Optional[str]:
        """Deadline from the refreshed cache, falling back to a bundled default."""
        return self.deadlines_cache.get(program_id, {}).get("deadline", default)

    def deadline_in(self, days: int) -> Optional[str]:
        """Absolute deadline `days` after the fire event, or None if undated."""
        if self.event_date is None:
            return None
        due = self.event_date + timedelta(days=days)
        if due < datetime.now(timezone.utc):
            return f"EXPIRED ({due.strftime('%Y-%m-%d')})"
        return due.strftime("%Y-%m-%d")


@dataclass(frozen=True)
class ProgramSpec:
    program_id: str
    name: str
    agency: str
    category: str  # livestock | crop | conservation | loan | mitigation | international | state
    link: str
    evaluate: Callable[[Ctx], Verdict]
    deadline: Dynamic = None
    deadline_trigger: Optional[str] = None
    estimated_value: Dynamic = None
    required_docs: Sequence[str] = ()
    notes: Dynamic = None
    requires_disaster_declaration: bool = False
    # Whether to echo the FEMA declaration lookup into `declaration_confirmed`.
    # Deliberately independent of `requires_disaster_declaration`: CDFA_ERL needs
    # a declaration but reports None, matching the behaviour this catalog replaced.
    report_declaration: bool = False
    # Programs shown only in these states. None means every state.
    states: Optional[tuple[str, ...]] = None
    # When ineligible, suppress deadline/value/docs/notes — they would only
    # mislead a farmer who cannot apply in the first place.
    blank_when_ineligible: bool = False


def resolve(value: Dynamic, ctx: Ctx) -> Optional[str]:
    return value(ctx) if callable(value) else value


# ---------------------------------------------------------------------------
# Reusable predicates
# ---------------------------------------------------------------------------

def _livestock_with(loss_key: str) -> Callable[[Ctx], bool]:
    return lambda c: c.profile["has_livestock"] and c.loss[loss_key]


def _elrp(ctx: Ctx) -> Verdict:
    if not (ctx.profile["has_livestock"] and ctx.loss["forage_loss"]):
        return "ineligible", "No livestock or no forage loss recorded"
    if ctx.profile["has_approved_lfp"]:
        return "confirmed", ("Has livestock, forage loss confirmed, approved LFP on file — "
                             "auto-payment triggered")
    return "likely", ("Has livestock and forage loss, but no approved LFP on file — "
                      "file LFP first to unlock ELRP auto-payment")


def _nap(ctx: Ctx) -> Verdict:
    if not (ctx.profile["has_crops"] and ctx.loss["crop_loss"]):
        return None
    if ctx.profile["has_federal_crop_insurance"]:
        return "ineligible", ("Hard exclusion: farmer has federal crop insurance — "
                              "NAP and crop insurance are mutually exclusive")
    if not ctx.profile["has_nap_coverage"]:
        return "ineligible", ("NAP coverage was not elected before the disaster event — "
                              "coverage must be purchased prior to loss")
    return "confirmed", ("Has crops, crop loss confirmed, NAP coverage elected pre-event, "
                         "no federal crop insurance")


def _efrp(ctx: Ctx) -> Verdict:
    if ctx.profile["has_forested_parcels"] and ctx.loss["forested_parcel_damage"]:
        return "confirmed", "Has non-industrial private forested parcels with wildfire damage"
    return "ineligible", "No non-industrial private forested parcels on this farm profile"


def _ewp(ctx: Ctx) -> Verdict:
    if ctx.loss["watershed_damage"]:
        return "confirmed", "Watershed damage from wildfire — formal request required within 60 days"
    return "ineligible", "No watershed damage recorded in loss summary"


def _sba(ctx: Ctx) -> Verdict:
    if not ctx.loss["economic_injury"]:
        return "ineligible", "No economic injury recorded"
    return ctx.decl_status(), (
        f"Small agricultural operation with economic injury from wildfire. {ctx.decl_note()}"
    )


def _international(us_reason: str, abroad_reason: str) -> Callable[[Ctx], Verdict]:
    def evaluate(ctx: Ctx) -> Verdict:
        if ctx.profile["country"] == "US":
            return "ineligible", us_reason
        return "check_required", abroad_reason
    return evaluate


def _calfire(ctx: Ctx) -> Verdict:
    if ctx.profile["has_forested_parcels"]:
        return "check_required", ("Non-industrial private forest landowner in CA — "
                                  "verify current grant cycle")
    return "ineligible", "No forested parcels on farm profile"


# ---------------------------------------------------------------------------
# The catalog. Order here is the order programs appear in the report before
# sorting; the sort is stable, so ties keep this sequence.
# ---------------------------------------------------------------------------

FSA = "USDA-FSA"
NRCS = "USDA-NRCS"

PROGRAMS: list[ProgramSpec] = [
    ProgramSpec(
        program_id="ELRP_2025",
        name="Emergency Livestock Relief Program (ELRP) — Wildfire",
        agency=FSA,
        category="livestock",
        link="https://www.fsa.usda.gov/resources/disaster-recovery/emergency-livestock-relief-program-elrp",
        evaluate=_elrp,
        deadline=lambda c: c.cached_deadline("ELRP_2025", "Nov 21, 2025"),
        deadline_trigger="Enrollment window Sep 15 – Nov 21, 2025",
        estimated_value="Up to $1 billion pool — individual payment auto-calculated",
        required_docs=["Approved LFP application", "Livestock inventory records"],
        notes="LFP application is the gateway — must be on file before ELRP payment is issued automatically",
        blank_when_ineligible=True,
    ),
    ProgramSpec(
        program_id="ELAP",
        name="Emergency Assistance for Livestock, Honeybees & Farm-Raised Fish (ELAP)",
        agency=FSA,
        category="livestock",
        link="https://www.fsa.usda.gov/programs-and-services/disaster-assistance-program/emergency-assist-for-livestock-honey-bees-fish/index",
        evaluate=lambda c: ("confirmed", "Has livestock with confirmed losses from wildfire")
        if _livestock_with("livestock_loss")(c) else None,
        deadline=lambda c: c.deadline_in(30),
        deadline_trigger="Notice of Loss must be filed within 30 days of event",
        estimated_value="Cost-based compensation — grazing, feed, water hauling",
        required_docs=["Notice of Loss (within 30 days)", "Feed/water cost receipts", "Livestock inventory"],
        notes="30-day Notice of Loss window is hard — do not delay",
    ),
    ProgramSpec(
        program_id="LFP",
        name="Livestock Forage Disaster Program (LFP)",
        agency=FSA,
        category="livestock",
        link="https://www.fsa.usda.gov/resources/disaster-recovery/livestock-forage-disaster-program-lfp",
        evaluate=lambda c: ("confirmed", "Has livestock with forage losses from wildfire")
        if _livestock_with("forage_loss")(c) else None,
        deadline=lambda c: c.cached_deadline("LFP", "Contact local FSA office"),
        estimated_value="Payment rate × number of eligible livestock",
        required_docs=["Livestock inventory", "Grazing lease or deed", "Evidence of forage loss"],
        notes="Filing LFP is required first — it is the gateway to ELRP auto-payment",
    ),
    ProgramSpec(
        program_id="LIP",
        name="Livestock Indemnity Program (LIP)",
        agency=FSA,
        category="livestock",
        link="https://www.fsa.usda.gov/programs-and-services/disaster-assistance-program/livestock-indemnity/index",
        evaluate=lambda c: ("confirmed", "Has livestock deaths above normal mortality from wildfire")
        if _livestock_with("livestock_deaths")(c) else None,
        deadline=lambda c: c.cached_deadline("LIP", "Mar 1, 2027 for 2026 losses"),
        deadline_trigger="Notice of Loss by Mar 1, 2027 for 2026 calendar year losses",
        estimated_value="75% of market value per head lost above normal mortality",
        required_docs=["Notice of Loss", "Livestock inventory pre-event",
                       "Death records / veterinary documentation"],
    ),
    ProgramSpec(
        program_id="NAP",
        name="Noninsured Crop Disaster Assistance Program (NAP)",
        agency=FSA,
        category="crop",
        link="https://www.fsa.usda.gov/programs-and-services/disaster-assistance-program/noninsured-assistance/index",
        evaluate=_nap,
        deadline=lambda c: c.cached_deadline("NAP", "Contact local FSA office"),
        estimated_value="55% of average market price for crop losses above 50% threshold",
        required_docs=["NAP coverage certification", "Production records", "Loss documentation"],
        blank_when_ineligible=True,
    ),
    ProgramSpec(
        program_id="ECP",
        name="Emergency Conservation Program (ECP)",
        agency=FSA,
        category="conservation",
        link="https://www.fsa.usda.gov/programs-and-services/conservation-programs/emergency-conservation/index",
        evaluate=lambda c: (
            "confirmed",
            f"Farmland infrastructure damage confirmed; cost-share rate: {c.ecp_rate}",
        ) if c.loss["infrastructure_damage"] else None,
        deadline=lambda c: c.cached_deadline("ECP", "Contact local FSA office"),
        estimated_value=lambda c: (
            f"{c.ecp_rate} cost-share on fencing repair, water restoration, debris removal"
        ),
        required_docs=["Damage assessment", "Cost estimates for repairs",
                       "Farm ownership / lease documentation"],
        notes=lambda c: "Cost-share is {} — {}".format(
            c.ecp_rate,
            "90% applies to underserved producers" if c.profile["underserved_producer"]
            else "to qualify for 90%, producer must be designated underserved",
        ),
    ),
    ProgramSpec(
        program_id="EFRP",
        name="Emergency Forest Restoration Program (EFRP)",
        agency=FSA,
        category="conservation",
        link="https://www.fsa.usda.gov/programs-and-services/conservation-programs/emergency-forest-restoration/index",
        evaluate=_efrp,
        deadline=lambda c: c.cached_deadline("EFRP", "Contact local FSA office"),
        estimated_value="Up to 75% cost-share on forest restoration practices",
        required_docs=["Forest management plan", "Damage documentation", "Land ownership records"],
        blank_when_ineligible=True,
    ),
    ProgramSpec(
        program_id="SDRP_2324",
        name="Supplemental Disaster Relief Program (SDRP) — 2023/2024",
        agency=FSA,
        category="crop",
        link="https://www.fsa.usda.gov/resources/programs/20232024-supplemental-disaster-assistance",
        evaluate=lambda c: (
            "check_required",
            "Has crops with losses — SDRP covers 2023 and 2024 weather events only; "
            "verify event year qualifies",
        ) if (c.profile["has_crops"] and c.loss["crop_loss"]) else None,
        deadline=lambda c: c.cached_deadline(
            "SDRP_2324", "Check FSA office — American Relief Act of 2025 program"),
        estimated_value="Crop revenue loss compensation — amount based on existing crop insurance data",
        required_docs=["Crop insurance records (Stage 1)", "Production records (Stage 2)",
                       "Evidence of revenue loss"],
        notes="Program covers 2023 and 2024 losses only — confirm event year is within scope",
    ),
    ProgramSpec(
        program_id="FSA_LOAN",
        name="FSA Emergency Farm Loans",
        agency=FSA,
        category="loan",
        link="https://www.fsa.usda.gov/programs-and-services/farm-loan-programs/emergency-farm-loans/index",
        evaluate=lambda c: (
            c.decl_status(),
            f"Farm/ranch with production or property losses. {c.decl_note()}",
        ),
        deadline=lambda c: c.deadline_in(243),  # ~8 months
        deadline_trigger="Apply within 8 months of disaster declaration date",
        estimated_value="Up to $500,000",
        required_docs=["Federal disaster declaration", "Farm financial records",
                       "Loss documentation", "Tax returns (3 years)"],
        notes="Requires federal disaster declaration — check FEMA declaration status first",
        requires_disaster_declaration=True,
        report_declaration=True,
    ),
    ProgramSpec(
        program_id="EQIP_FIRE",
        name="Environmental Quality Incentives Program (EQIP) — Wildfire",
        agency=NRCS,
        category="conservation",
        link="https://www.nrcs.usda.gov/programs-and-initiatives/eqip-environmental-quality-incentives",
        evaluate=lambda c: (
            "confirmed",
            f"Has eligible land types: {', '.join(c.profile['land_types'])}",
        ) if c.profile["land_types"] else None,
        deadline=lambda c: c.cached_deadline(
            "EQIP_FIRE", "Contact local NRCS office — rolling applications"),
        estimated_value="Cost-share payments for approved conservation practices",
        required_docs=["Farm plan", "NRCS application", "Land documentation"],
    ),
    ProgramSpec(
        program_id="EWP",
        name="Emergency Watershed Protection (EWP) Program",
        agency=NRCS,
        category="conservation",
        link="https://www.nrcs.usda.gov/programs-and-initiatives/ewp-emergency-watershed-protection-program",
        evaluate=_ewp,
        deadline=lambda c: c.deadline_in(60),
        deadline_trigger="Formal request to state conservationist within 60 days of disaster",
        estimated_value="Up to 75% of restoration costs (90% for limited-resource/underserved)",
        required_docs=["Formal request to NRCS state conservationist", "Site damage documentation"],
        notes="Request must be submitted by a project sponsor (local government, tribe, or similar) — "
              "individual farmers apply through a sponsor",
        blank_when_ineligible=True,
    ),
    ProgramSpec(
        program_id="FEMA_IA",
        name="FEMA Individual Assistance (IA)",
        agency="FEMA",
        category="mitigation",
        link="https://www.disasterassistance.gov",
        evaluate=lambda c: (
            c.decl_status(),
            f"Farmers qualify as individuals/households. {c.decl_note()}",
        ),
        deadline_trigger="Apply as soon as Presidential Major Disaster Declaration is issued",
        estimated_value="Grants for home/property repair, essential items, serious disaster needs",
        required_docs=["Disaster declaration number", "Proof of ownership/occupancy",
                       "Insurance documentation"],
        notes="Apply at disasterassistance.gov — requires Presidential Major Disaster Declaration",
        requires_disaster_declaration=True,
        report_declaration=True,
    ),
    ProgramSpec(
        program_id="FEMA_FMAG",
        name="FEMA Fire Management Assistance Grant (FMAG)",
        agency="FEMA",
        category="mitigation",
        link="https://www.fema.gov/assistance/public/fire-management-assistance",
        evaluate=lambda c: (
            "check_required",
            "Not direct farmer aid — issued to state/local/tribal governments; activates other programs",
        ),
        estimated_value="Varies — state-level grant",
        notes="FMAG activates additional state-level disaster programs — "
              "check if your state has an active FMAG declaration",
    ),
    ProgramSpec(
        program_id="FEMA_HMGP",
        name="FEMA Hazard Mitigation Grant Program (HMGP) — Post Fire",
        agency="FEMA",
        category="mitigation",
        link="https://www.fema.gov/grants/mitigation/hazard-mitigation",
        evaluate=lambda c: (
            c.decl_status("check_required"),
            f"Long-term mitigation projects (firebreaks, etc.). {c.decl_note()}",
        ),
        deadline=lambda c: c.deadline_in(365),
        deadline_trigger="Available up to 12 months after presidentially-declared major disaster",
        estimated_value="Varies by project scope",
        required_docs=["Disaster declaration number", "Project proposal", "Cost-benefit analysis"],
        requires_disaster_declaration=True,
        report_declaration=True,
    ),
    ProgramSpec(
        program_id="SBA_EIDL",
        name="SBA Economic Injury Disaster Loans (EIDL)",
        agency="SBA",
        category="loan",
        link="https://www.sba.gov/funding-programs/disaster-assistance",
        evaluate=_sba,
        deadline_trigger="Apply after SBA disaster declaration for the area",
        estimated_value="Up to $2,000,000",
        required_docs=["SBA disaster declaration", "Business financial statements (3 years)",
                       "Personal financial statement", "Tax returns"],
        notes="Covers cash flow losses, not physical property — complements FSA Emergency Loans",
        requires_disaster_declaration=True,
        report_declaration=True,
    ),
    ProgramSpec(
        program_id="FAO_FIRE_HUB",
        name="FAO Global Fire Management Hub",
        agency="UN/FAO",
        category="international",
        link="https://www.fao.org/partnerships/fire-hub/en",
        evaluate=_international(
            "FAO Fire Hub is a coordination body for international farmers — "
            "US farmers should use USDA programs above",
            "International farmer — contact FAO national focal point",
        ),
        notes="Policy/coordination body — not a direct aid program for US farmers",
    ),
    ProgramSpec(
        program_id="GCF_FAO",
        name="Green Climate Fund (GCF) via FAO",
        agency="UN/FAO",
        category="international",
        link="https://www.greenclimate.fund/ae/fao",
        evaluate=_international(
            "Access is through national government applications only — "
            "not direct farmer enrollment",
            "International — apply through national government",
        ),
        estimated_value="Tens of millions per project",
        notes="Structural constraint: no direct farmer application path regardless of geography",
    ),
    ProgramSpec(
        program_id="CDFA_ERL",
        name="CA Dept of Food & Agriculture — Emergency Relief Programs",
        agency="CDFA",
        category="state",
        link="https://www.cdfa.ca.gov/grants/",
        states=("CA",),
        evaluate=lambda c: (
            "check_required",
            "CA farm with wildfire loss — governor's emergency declaration status unknown; "
            "verify at CDFA",
        ),
        deadline_trigger="Check CDFA website — deadlines vary by program cycle",
        estimated_value="Varies by program",
        required_docs=["CA farm registration", "Loss documentation",
                       "Governor's emergency declaration number"],
        notes="CA state program deadlines are variable — check CDFA directly after any "
              "governor's emergency declaration",
        requires_disaster_declaration=True,
    ),
    ProgramSpec(
        program_id="CALFIRE_FRAP",
        name="CAL FIRE — Forest Health Grants",
        agency="CAL FIRE",
        category="state",
        link="https://www.fire.ca.gov/grants",
        states=("CA",),
        evaluate=_calfire,
        deadline_trigger="Rolling applications — check CAL FIRE grants portal",
        estimated_value="Varies by project",
        required_docs=["Timber Production Zone designation or equivalent",
                       "Reforestation/fire resilience project plan"],
    ),
    ProgramSpec(
        program_id="CDFA_OEFI",
        name="CA Office of Emergency Food and Farming Infrastructure (OEFI)",
        agency="CDFA",
        category="state",
        link="https://www.cdfa.ca.gov/oefi/",
        states=("CA",),
        evaluate=lambda c: (
            "check_required",
            "Small/mid-scale CA farm with food system disruption from disaster — "
            "verify current funding cycle",
        ),
        deadline_trigger="Check CDFA OEFI website for open solicitations",
        estimated_value="Varies by project",
        required_docs=["CA farm registration", "Evidence of disaster-related disruption",
                       "Project proposal"],
    ),
    ProgramSpec(
        program_id="CA_EDD_DISASTER",
        name="CA EDD Disaster Unemployment Assistance",
        agency="CA EDD",
        category="state",
        link="https://edd.ca.gov/en/unemployment/disaster/",
        states=("CA",),
        evaluate=lambda c: (
            "check_required",
            "Covers self-employed farmers who lost work due to disaster — "
            "verify active DUA period for this disaster",
        ),
        deadline_trigger="Apply within 30 days of DUA announcement",
        estimated_value="Weekly benefit based on prior earnings",
        required_docs=["Proof of self-employment or farm income", "Disaster declaration number",
                       "Evidence of lost work"],
        requires_disaster_declaration=True,
        report_declaration=True,
    ),
]
