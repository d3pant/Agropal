"""Visual layer for the action briefing PDF: colors, fonts, paragraph styles.

Split out from report_agent.py so the agent file reads as "what goes in the
briefing" and this one as "what it looks like". Nothing here knows about
forecaster, crop, or livestock data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph

from erpc.common import get_logger

logger = get_logger("report_style")

# Theme — kept minimal, two-color (green headings, near-black body).
GREEN = colors.HexColor("#2d6a4f")
TEXT = colors.HexColor("#1a1a1a")
TEXT2 = colors.HexColor("#5a5a5a")
TEXT3 = colors.HexColor("#9a9a9a")


# --- Font registration per language ----------------------------------------
#
# Reportlab's default Helvetica is a Type 1 font with WinAnsi encoding only —
# it has no glyphs for CJK, Arabic, Devanagari, or even the full Vietnamese
# Latin Extended Additional set. Without registering a Unicode-capable font,
# translated text renders as boxes (■). Strategy:
#
#   - English and Latin-1 languages (es, pt, fr, tl):  Helvetica (default)
#   - Simplified Chinese:  STSong-Light (built-in CID font, no external file)
#   - Korean:              HYSMyeongJo-Medium (built-in CID font)
#   - Vietnamese, Arabic, Hindi, and any unhandled non-Latin language:
#                          Arial Unicode TTF (covers ~50,000 glyphs incl. CJK,
#                          Devanagari, Arabic, Latin Extended Additional)
#
# Bold variants for non-Latin fonts: most CJK/Unicode fonts on macOS don't ship
# a separate bold weight. We register the same font as both regular and bold so
# `<b>` markup in Paragraphs doesn't fail, even though it won't render bolder.

LATIN1_LANGUAGES = {"en", "es", "pt", "fr", "tl"}

# Built-in CID fonts, by language. These need no font file on disk.
_CID_FONTS = {"zh-CN": "STSong-Light", "ko": "HYSMyeongJo-Medium"}

_ARIAL_UNICODE_PATHS = (
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
)

_FONT_REGISTRY: dict[str, str] = {}


def _register_family(name: str) -> None:
    """Register `name` as its own bold/italic variants (see note above)."""
    pdfmetrics.registerFontFamily(
        name, normal=name, bold=name, italic=name, boldItalic=name,
    )
    _FONT_REGISTRY[name] = name


def _register_arial_unicode() -> Optional[str]:
    if "ArialUnicode" in _FONT_REGISTRY:
        return _FONT_REGISTRY["ArialUnicode"]
    for path in _ARIAL_UNICODE_PATHS:
        if not Path(path).exists():
            continue
        try:
            pdfmetrics.registerFont(TTFont("ArialUnicode", path))
            _register_family("ArialUnicode")
            return "ArialUnicode"
        except Exception as exc:
            logger.warning("Failed to register Arial Unicode at %s: %s", path, exc)
    return None


def _register_cid(name: str) -> Optional[str]:
    if name in _FONT_REGISTRY:
        return _FONT_REGISTRY[name]
    try:
        pdfmetrics.registerFont(UnicodeCIDFont(name))
        _register_family(name)
        return name
    except Exception as exc:
        logger.warning("Failed to register CID font %s: %s", name, exc)
        return None


def setup_font_for_lang(target_lang: str) -> str:
    """Register a Unicode-capable font for the language; return the font name."""
    if target_lang in LATIN1_LANGUAGES:
        return "Helvetica"

    cid_name = _CID_FONTS.get(target_lang)
    if cid_name and (registered := _register_cid(cid_name)):
        return registered

    # vi, hi, ar, and any CJK fallback: Arial Unicode
    if arial := _register_arial_unicode():
        return arial

    logger.warning(
        "No Unicode font available; falling back to Helvetica — "
        "non-Latin glyphs will render as boxes"
    )
    return "Helvetica"


# --- Paragraph styles ------------------------------------------------------

# name -> (parent stylesheet key, font size, leading, color, use bold face,
#          space before or None to inherit, space after, extra kwargs)
_STYLE_SPECS: dict[str, tuple] = {
    "title":     ("Heading1", 22,   28,   GREEN, True,  None, 2,  {}),
    "subtitle":  ("Normal",   11,   15,   TEXT2, False, None, 14, {}),
    "h1":        ("Heading2", 15,   20,   GREEN, True,  18, 8,  {}),
    "h2":        ("Heading3", 12,   16,   TEXT,  True,  10, 4,  {}),
    "lead":      ("Normal",   11,   16,   TEXT,  True,  None, 8,  {}),
    "body":      ("Normal",   10.5, 15,   TEXT,  False, None, 8,  {}),
    "bullet":    ("Normal",   10.5, 15,   TEXT,  False, None, 3,
                  {"leftIndent": 18, "bulletIndent": 4}),
    "checkitem": ("Normal",   10.5, 15,   TEXT,  False, None, 2, {"leftIndent": 22}),
    "small":     ("Normal",   9,    12,   TEXT2, False, None, 8,  {}),
}


def build_styles(font_name: str = "Helvetica") -> dict[str, ParagraphStyle]:
    """Build the paragraph styles using the supplied font.

    Helvetica gets Helvetica-Bold for headings; any other font reuses its own
    name, since CID and Arial Unicode have no separate bold weight.
    """
    bold_name = "Helvetica-Bold" if font_name == "Helvetica" else font_name
    base = getSampleStyleSheet()
    styles = {}
    for name, (parent, size, leading, color, bold, before, after, extra) in _STYLE_SPECS.items():
        if before is not None:
            extra = {**extra, "spaceBefore": before}
        styles[name] = ParagraphStyle(
            name,
            parent=base[parent],
            fontSize=size,
            leading=leading,
            textColor=color,
            fontName=bold_name if bold else font_name,
            spaceAfter=after,
            **extra,
        )
    return styles


def para(text: str, styles, style: str = "body") -> Paragraph:
    return Paragraph(text, styles[style])


def bullets(items: list[str], styles, style: str = "bullet") -> list[Paragraph]:
    return [Paragraph("• " + item, styles[style]) for item in items]


# --- Page furniture --------------------------------------------------------

def draw_page_furniture(canvas, doc) -> None:
    """Footer drawn on every page. Signature is fixed by reportlab."""
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(TEXT3)
    canvas.drawString(0.6 * inch, 0.4 * inch, "Reeboot the Earth — Action Briefing")
    canvas.drawRightString(
        letter[0] - 0.6 * inch, 0.4 * inch, f"Page {canvas.getPageNumber()}"
    )
    canvas.restoreState()
