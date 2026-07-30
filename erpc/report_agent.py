"""Report Agent — final ERPC PDF.

Combines outputs from the forecaster, crop, livestock, econ, policy, and
insurance agents into a single human-readable action briefing for the farmer.
Includes a static "evacuation go-bag" checklist.

This is a *text-heavy* briefing: prose paragraphs and bulleted lists rather than
tables or KPI grids. That is deliberate — Google Translate produces much better
output when given full sentences with surrounding context than isolated cell
values.

Usage:
    python -m erpc.report_agent
    python -m erpc.report_agent --lang es
    python -m erpc.report_agent --lang vi --output briefing_vi.pdf

Translation uses deep-translator (Google web endpoint, no API key needed).
Output: forecaster/output/action_briefing[_<lang>].pdf
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

from erpc.common import (
    ECON_REPORT, FARM_CONFIG, FILLED_PDF, LIVESTOCK_ERPC_MSG, LIVESTOCK_STATUS,
    OUTPUT_DIR, POLICY_REPORT, STATUS_JSON, get_logger, load_crop_output, load_json,
)
from erpc.report_style import bullets, build_styles, draw_page_furniture, para, setup_font_for_lang
from erpc.report_text import (
    EMERGENCY_CHECKLIST, EMERGENCY_CONTACTS, SUPPORTED_LANGUAGES, Translator,
    fmt_hours, fmt_money,
)

logger = get_logger("report_agent")

# ── Data loading ─────────────────────────────────────────────────────────────

# ── Section builders ─────────────────────────────────────────────────────────

def _section_summary(status, t, styles) -> list:
    # Use title case for the threat level word inside prose. All-caps tokens
    # like "CRITICAL" sometimes confuse the translator — Vietnamese, for
    # example, maps the all-caps form to "great/wonderful" instead of
    # "critical/serious". Title case keeps the emphasis but reads as a normal
    # English adjective.
    threat_raw = status.get("threat_level") or "Unknown"
    threat = threat_raw.title()
    fire = status.get("nearest_fire") or {}
    impact = (status.get("spread_prediction") or {}).get("time_to_farm") or {}
    fwi = status.get("fwi_index")
    wind = status.get("wind_speed_kmh")
    wind_dir = status.get("wind_direction_degrees")
    temp = status.get("temperature_c")
    humidity = status.get("humidity_percent")
    gate_reason = status.get("gate_condition_reason")

    elems = [Paragraph(t("Current Threat Snapshot"), styles["h1"])]

    fire_name = fire.get("name") or "no active fire detected nearby"
    fire_dist = fire.get("distance_km")
    impact_hours = impact.get("hours") if isinstance(impact, dict) else None

    if fire_dist is not None and impact_hours is not None:
        lead = (
            f"Your farm is currently at the <b>{threat}</b> threat level. "
            f"The nearest detected wildfire is {fire_name}, located {fire_dist:.1f} kilometers away. "
            f"Based on current wind and fuel conditions, fire could reach your farm in {fmt_hours(impact_hours)}."
        )
    elif fire_dist is not None:
        lead = (
            f"Your farm is currently at the <b>{threat}</b> threat level. "
            f"The nearest detected wildfire is {fire_name}, located {fire_dist:.1f} kilometers away. "
            f"No precise time-to-impact estimate is available at the moment."
        )
    else:
        lead = (
            f"Your farm is currently at the <b>{threat}</b> threat level. "
            f"There is {fire_name} at the moment."
        )
    elems.append(para(t(lead), styles, "lead"))

    weather_parts = []
    if isinstance(wind, (int, float)) and isinstance(wind_dir, (int, float)):
        weather_parts.append(f"Wind is blowing at {wind:.0f} kilometers per hour from {wind_dir:.0f} degrees")
    if isinstance(temp, (int, float)):
        weather_parts.append(f"the temperature is {temp:.0f} degrees Celsius")
    if isinstance(humidity, (int, float)):
        weather_parts.append(f"relative humidity is {humidity:.0f} percent")
    if isinstance(fwi, (int, float)):
        weather_parts.append(f"the Fire Weather Index is {fwi:.1f}")
    if weather_parts:
        weather = "Weather conditions: " + ", ".join(weather_parts) + "."
        elems.append(para(t(weather), styles))

    if gate_reason:
        elems.append(para(t(f"Gate condition: {gate_reason}"), styles, "small"))

    return elems


def _section_livestock(livestock_status, erpc, t, styles) -> list:
    elems = [Paragraph(t("Livestock Plan"), styles["h1"])]
    pens = (livestock_status or {}).get("pens", []) or []
    cost_opt = (erpc or {}).get("cost_optimization", {}) or {}

    total = cost_opt.get("total_animals_at_risk", 0)
    can_evac = cost_opt.get("animals_can_evacuate", 0)
    save_value = cost_opt.get("value_can_save_usd", 0) or 0
    potential_loss = cost_opt.get("potential_loss_usd", 0) or 0

    if total == 0 and not pens:
        elems.append(para(t("No livestock evacuation plan has been generated yet."), styles))
        return elems

    intro = (
        f"You have {total} animals at risk across all pens. Of those, {can_evac} can be "
        f"safely evacuated using available trailers and routes, protecting an estimated "
        f"{fmt_money(save_value)} in livestock value."
    )
    if potential_loss > 0:
        intro += (
            f" If evacuation cannot be fully completed, the potential loss is "
            f"{fmt_money(potential_loss)}."
        )
    elems.append(para(t(intro), styles, "lead"))

    if pens:
        elems.append(Paragraph(t("Per-pen evacuation plan"), styles["h2"]))
        points = []
        for p in pens[:20]:
            site = (p.get("assigned_evac_site") or {}).get("name") or "an unspecified evacuation site"
            decision = (p.get("decision") or "monitor").replace("_", " ")
            reason = (p.get("decision_reason") or "").strip()
            species = p.get("species") or "livestock"
            pen_id = p.get("pen_id") or "pen"
            sentence = f"<b>{pen_id}</b> ({species}): plan is to {decision} to {site}."
            if reason:
                sentence += f" {reason}."
            points.append(t(sentence))
        elems += bullets(points, styles)

    return elems


def _section_crop(crop, t, styles) -> list:
    elems = [Paragraph(t("Crop Plan"), styles["h1"])]
    decisions = crop.get("field_decisions") or crop.get("task4") or []
    impacts = (crop.get("economic_impact") or crop.get("task2") or {}).get("crop_destructions", []) or []
    hydration = crop.get("hydration_strategy") or crop.get("task3") or []

    if not (decisions or impacts or hydration):
        elems.append(para(
            t("No crop actions are required at the current threat level. The crop agent did not flag any field as economically impacted."),
            styles,
        ))
        return elems

    # Decisions paragraph
    if decisions:
        elems.append(Paragraph(t("Field decisions"), styles["h2"]))
        intro = f"The crop agent reviewed {len(decisions)} field" + ("s" if len(decisions) != 1 else "") + " and recommends the following actions."
        elems.append(para(t(intro), styles))
        points = []
        for d in decisions:
            fid = d.get("field_id", "field")
            crop_cat = d.get("crop_category", "crop")
            maturity = d.get("maturity_pct", 0)
            arrival = d.get("fire_arrival_hours", 0)
            decision = d.get("decision", "monitor")
            reason = (d.get("reason") or "").strip()
            sent = (
                f"<b>{fid}</b> ({crop_cat}, {maturity}% mature): {decision}. "
                f"Fire is estimated to arrive in {fmt_hours(arrival)}."
            )
            if reason:
                sent += f" {reason}."
            points.append(t(sent))
        elems += bullets(points, styles)

    # Economic impact
    if impacts:
        elems.append(Paragraph(t("Economic impact per field"), styles["h2"]))
        points = []
        for d in impacts:
            fid = d.get("field_id", "field")
            crop_cat = d.get("crop_category", "crop")
            acres = d.get("size_acres", 0)
            adj_loss = d.get("confidence_adjusted_loss_usd", 0)
            decision = (d.get("task4_decision") or "no action").lower()
            sent = (
                f"<b>{fid}</b> ({crop_cat}, {acres:.1f} acres): estimated loss is "
                f"{fmt_money(adj_loss)} if the field is left unprotected. "
                f"Recommended action: {decision}."
            )
            points.append(t(sent))
        elems += bullets(points, styles)

    # Hydration / firebreaks
    if hydration:
        elems.append(Paragraph(t("Hydration and firebreak schedule"), styles["h2"]))
        points = []
        for h in hydration:
            fid = h.get("field_id", "field")
            tech = h.get("technique", "monitor")
            urgency = (h.get("urgency") or "scheduled").lower()
            arr = h.get("hours_to_arrival", 0)
            sent = (
                f"<b>{fid}</b>: apply {tech}. Urgency is {urgency}. "
                f"Fire is estimated to arrive in {fmt_hours(arr)}."
            )
            points.append(t(sent))
        elems += bullets(points, styles)

    return elems


def _section_financial(econ, t, styles) -> list:
    elems = [Paragraph(t("Financial Snapshot"), styles["h1"])]
    exp = (econ or {}).get("financial_exposure", {}) or {}
    actions = (econ or {}).get("action_queue", []) or []
    infeasible = (econ or {}).get("infeasible_actions", []) or []

    total = exp.get("total_exposure_usd", 0) or 0
    crop_loss = exp.get("crop_loss_total_usd", 0) or 0
    livestock = exp.get("livestock_at_risk_usd", 0) or 0
    opportunity = exp.get("opportunity_cost_usd", 0) or 0

    intro = (
        f"Your total estimated financial exposure if no protective action is taken is "
        f"{fmt_money(total)}. This breaks down as {fmt_money(crop_loss)} in potential crop losses, "
        f"{fmt_money(livestock)} in livestock value at risk, and {fmt_money(opportunity)} in "
        f"opportunity cost from disrupted operations."
    )
    elems.append(para(t(intro), styles, "lead"))

    if actions:
        elems.append(Paragraph(t("Recommended actions, ranked by return on investment"), styles["h2"]))
        points = []
        for i, a in enumerate(actions[:8], 1):
            roi = a.get("roi", 0)
            roi_str = f"{roi:.1f} times the cost" if isinstance(roi, (int, float)) and roi > 0 else "value to be confirmed"
            urgency = (a.get("urgency") or "scheduled").lower()
            avoided = a.get("confidence_adjusted_loss_avoided_usd", 0)
            cost = a.get("estimated_action_cost_usd", 0)
            desc = (a.get("action_description") or "").strip()
            sent = (
                f"<b>Action {i}</b> ({urgency}): {desc}. "
                f"This protects roughly {fmt_money(avoided)} at an estimated cost of "
                f"{fmt_money(cost)} — a return of {roi_str}."
            )
            points.append(t(sent))
        elems += bullets(points, styles)

    if infeasible:
        elems.append(Paragraph(t("Blocked actions"), styles["h2"]))
        elems.append(para(
            t("These actions cannot be completed under current conditions or with available resources:"),
            styles,
        ))
        points = []
        for a in infeasible[:5]:
            aid = a.get("action_id") or "action"
            reason = (a.get("infeasibility_reason") or "blocker not specified").strip()
            points.append(t(f"<b>{aid}</b>: {reason}."))
        elems += bullets(points, styles)

    return elems


def _section_aid(policy, insurance_filled: bool, t, styles) -> list:
    elems = [Paragraph(t("Aid and Insurance"), styles["h1"])]

    if insurance_filled:
        elems.append(para(
            t(
                "Your USDA CCC-576 Notice of Loss has been pre-filled with farm and disaster data. "
                "You must file it within 30 days of the loss event at your local Farm Service Agency office. "
                "The pre-filled PDF is available in the Insurance section of your dashboard."
            ),
            styles,
        ))

    eligible = (policy or {}).get("eligible_programs", []) or []
    if eligible:
        elems.append(Paragraph(t("Eligible aid programs"), styles["h2"]))
        elems.append(para(
            t(f"The policy agent identified {len(eligible)} program" + ("s" if len(eligible) != 1 else "") + " for which your farm is likely eligible. The most relevant are listed below."),
            styles,
        ))
        points = []
        for p in eligible[:6]:
            name = p.get("name") or "Program"
            agency = p.get("agency") or "Agency"
            deadline = p.get("deadline") or "ongoing"
            status = (p.get("eligibility_status") or "likely").lower()
            sent = (
                f"<b>{name}</b> ({agency}): eligibility status is {status}. "
                f"Filing deadline: {deadline}."
            )
            points.append(t(sent))
        elems += bullets(points, styles)
    else:
        elems.append(para(
            t("No policy data is available yet. Run the policy agent to identify federal, state, and local aid programs you may qualify for."),
            styles,
        ))

    return elems


def _section_checklist(t, styles) -> list:
    elems = [PageBreak(), Paragraph(t("Evacuation Go-Bag Checklist"), styles["h1"])]
    elems.append(para(
        t(
            "Pack these items now so you can leave the property within fifteen minutes if a critical "
            "threat is declared. Keep the bag near your primary exit, and rotate perishable items "
            "(food, medications, water) every six months."
        ),
        styles, "lead",
    ))
    for cat_title, items in EMERGENCY_CHECKLIST:
        elems.append(Paragraph(t(cat_title), styles["h2"]))
        for item in items:
            elems.append(Paragraph("☐  " + t(item), styles["checkitem"]))
        elems.append(Spacer(1, 4))
    return elems


def _section_contacts(livestock_status, t, styles) -> list:
    elems = [Paragraph(t("Emergency Contacts"), styles["h1"])]
    elems.append(para(
        t("Save these numbers in your phone now. Print this page and keep a paper copy in your go-bag in case your phone dies during evacuation."),
        styles,
    ))

    items = list(EMERGENCY_CONTACTS)

    pens = (livestock_status or {}).get("pens", []) or []
    sites_seen = set()
    for p in pens[:10]:
        site = p.get("assigned_evac_site") or {}
        name = site.get("name")
        if name and name not in sites_seen:
            sites_seen.add(name)
            lat = site.get("lat")
            lon = site.get("lon")
            coords = f" (located at {lat}, {lon})" if lat is not None and lon is not None else ""
            items.append(f"Assigned evacuation site: {name}{coords}")

    elems += bullets([t(s) for s in items], styles)
    return elems


# ── Build report ─────────────────────────────────────────────────────────────

def build_report(target_lang: str = "en", output_path: Optional[Path] = None) -> Path:
    farm_config = load_json(FARM_CONFIG)
    status = load_json(STATUS_JSON)
    livestock_status = load_json(LIVESTOCK_STATUS)
    erpc = load_json(LIVESTOCK_ERPC_MSG)
    crop, _ = load_crop_output()
    econ = load_json(ECON_REPORT)
    policy = load_json(POLICY_REPORT)
    insurance_filled = FILLED_PDF.exists()

    farm_name = farm_config.get("farm_name") or status.get("farm_name") or "Farm"
    timestamp = datetime.now(timezone.utc).strftime("%B %d, %Y at %H:%M UTC")

    if target_lang not in SUPPORTED_LANGUAGES:
        logger.warning("Unsupported language %s — falling back to English", target_lang)
        target_lang = "en"
    translator = Translator(target_lang)
    t = translator.t

    if output_path is None:
        suffix = "" if target_lang == "en" else f"_{target_lang}"
        output_path = OUTPUT_DIR / f"action_briefing{suffix}.pdf"
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    font_name = setup_font_for_lang(target_lang)
    logger.info("Using font %s for language %s", font_name, target_lang)
    styles = build_styles(font_name)
    doc = SimpleDocTemplate(
        str(output_path), pagesize=letter,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        topMargin=0.7 * inch, bottomMargin=0.7 * inch,
        title="Action Briefing",
    )

    flow = []
    flow.append(Paragraph(t("Wildfire Action Briefing"), styles["title"]))
    subtitle = (
        f"Prepared for <b>{farm_name}</b>. Generated on {timestamp}."
    )
    if target_lang != "en":
        subtitle += f" Language: <b>{SUPPORTED_LANGUAGES[target_lang]}</b>."
    flow.append(para(t(subtitle), styles, "subtitle"))

    flow += _section_summary(status, t, styles)
    flow += _section_livestock(livestock_status, erpc, t, styles)
    flow += _section_crop(crop, t, styles)
    flow += _section_financial(econ, t, styles)
    flow += _section_aid(policy, insurance_filled, t, styles)
    flow += _section_checklist(t, styles)
    flow += _section_contacts(livestock_status, t, styles)

    doc.build(flow, onFirstPage=draw_page_furniture, onLaterPages=draw_page_furniture)
    logger.info("Wrote %s (%d translated strings cached)", output_path, len(translator.cache))
    return output_path


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate the comprehensive action briefing PDF")
    parser.add_argument("--lang", default="en", choices=list(SUPPORTED_LANGUAGES.keys()),
                        help="Target language (default: en)")
    parser.add_argument("--output", help="Output PDF path")
    args = parser.parse_args()

    out = build_report(target_lang=args.lang, output_path=args.output)
    print(f"\n  Action briefing written: {out}")
    print(f"  Language: {SUPPORTED_LANGUAGES[args.lang]}")


if __name__ == "__main__":
    main()
