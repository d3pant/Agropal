"""Language, translation, and static copy for the action briefing.

Holds everything the briefing *says* that does not depend on live farm data:
the supported language list, the translator, the value-to-prose formatters, and
the static checklist and contact copy.
"""

from __future__ import annotations

import re

from erpc.common import get_logger

logger = get_logger("report_text")

SUPPORTED_LANGUAGES = {
    "en": "English",
    "es": "Spanish",
    "zh-CN": "Chinese (Simplified)",
    "vi": "Vietnamese",
    "tl": "Tagalog (Filipino)",
    "ko": "Korean",
    "ar": "Arabic",
    "hi": "Hindi",
    "fr": "French",
    "pt": "Portuguese",
}

RTL_LANGUAGES = {"ar", "he", "fa", "ur"}

# Matches any HTML-ish tag, e.g. the <b> markup used in Paragraph text.
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")


# --- Translation -----------------------------------------------------------

def post_process(text: str, target_lang: str) -> str:
    """Apply language-specific post-processing to translated text.

    For RTL scripts, run arabic-reshaper + python-bidi so connected forms and
    visual order are correct in left-to-right PDF rendering. We must strip HTML
    markup first — the bidi algorithm reorders chars across `<b>` tag
    boundaries, producing malformed XML that reportlab's parser rejects.
    """
    if not text or target_lang not in RTL_LANGUAGES:
        return text
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        return get_display(arabic_reshaper.reshape(_TAG_RE.sub("", text)))
    except Exception as exc:
        logger.warning("RTL post-processing failed: %s", exc)
        return text


class Translator:
    """Lazy on-demand translator with per-string cache.

    Falls back to the English text on any failure so the report always renders.
    For RTL languages the result is run through `post_process`.
    """

    def __init__(self, target_lang: str = "en"):
        self.target = target_lang
        self.cache: dict[str, str] = {}
        self._gt = None
        if target_lang and target_lang != "en":
            try:
                from deep_translator import GoogleTranslator
                self._gt = GoogleTranslator(source="en", target=target_lang)
            except Exception as exc:
                logger.warning("Translator unavailable for %s: %s", target_lang, exc)

    def t(self, text: str) -> str:
        if not text or self.target == "en" or self._gt is None:
            return text
        if text in self.cache:
            return self.cache[text]
        try:
            translated = self._gt.translate(text) or text
        except Exception:
            translated = text
        self.cache[text] = post_process(translated, self.target)
        return self.cache[text]


# --- Value formatters ------------------------------------------------------

def fmt_money(n) -> str:
    """Inline money string. Numbers are kept as digits — translators preserve
    these for most languages; that's intentional so amounts stay unambiguous."""
    if not isinstance(n, (int, float)) or n == 0:
        return "$0"
    if abs(n) >= 1_000_000:
        return f"${n / 1_000_000:.2f} million"
    if abs(n) >= 1_000:
        return f"${n / 1_000:.1f} thousand"
    return f"${n:,.0f}"


def fmt_hours(h) -> str:
    if not isinstance(h, (int, float)):
        return "—"
    if h < 1:
        return f"{int(h * 60)} minutes"
    if h < 48:
        return f"{h:.1f} hours"
    return f"{h / 24:.1f} days"


# --- Static copy -----------------------------------------------------------

EMERGENCY_CHECKLIST = [
    ("Personal", [
        "Government ID, passport, driver's license",
        "Insurance cards (health, property, crop, livestock)",
        "Cash and credit or debit cards",
        "Phone and charger, including a car charger or power bank",
        "Prescriptions and a seven-day supply of medication",
        "Eyeglasses or hearing aids",
    ]),
    ("Documents (originals or photocopies)", [
        "Property deeds and mortgage papers",
        "Livestock registration and health or vaccination records",
        "Crop insurance policies and recent receipts",
        "Tax records for the last three years",
        "Birth certificates, marriage certificate, and social security cards",
        "Vehicle titles and registration",
        "USB drive or cloud backup of farm records",
    ]),
    ("Animals", [
        "Halters, leads, and transport crates",
        "Portable water containers and a three-day feed supply",
        "Veterinary records and any required medications",
        "Recent photos of each animal as proof of ownership",
        "Pet carriers and leashes for dogs and cats",
    ]),
    ("Farm records", [
        "Backup of farm management software or spreadsheets",
        "Field maps and irrigation schedules",
        "Equipment inventory and serial numbers",
        "Contact list with veterinarians, suppliers, and neighboring farms",
    ]),
    ("Vehicle preparation", [
        "Fuel tank filled. Do this now, not at evacuation time.",
        "Tire pressure checked, with the spare in good condition",
        "Emergency kit with water, food, and first-aid supplies for three days",
        "Blankets, a change of clothes, and sturdy shoes",
        "Flashlight and extra batteries",
    ]),
    ("Do not forget", [
        "Family photos and irreplaceable items",
        "Comfort items for children",
        "List of evacuation contact addresses",
        "Paper maps of evacuation routes in case GPS fails",
    ]),
]

# NOTE: these are California/San Diego specific. A farm set up elsewhere gets
# the wrong state hotline and FSA number — make these lookups keyed on
# farm_config location before shipping outside CA.
EMERGENCY_CONTACTS = [
    "Emergency services: 911",
    "CAL FIRE information line: 1-800-540-2722",
    "San Diego County Farm Service Agency: (760) 745-3061",
    "California wildfire updates: https://www.fire.ca.gov/incidents/",
]
