"""Fetch crop yield and price data from USDA NASS QuickStats."""

import os
import requests
from datetime import datetime

USDA_NASS_BASE = "https://quickstats.nass.usda.gov/api/api_GET/"

# NASS commodity name mapping (exact strings NASS accepts)
NASS_COMMODITY_MAP = {
    "wheat":        "WHEAT",
    "corn":         "CORN",
    "soybeans":     "SOYBEANS",
    "cotton":       "COTTON",
    "alfalfa":      "HAY, ALFALFA",
    "grapes":       "GRAPES",
    "almonds":      "ALMONDS",
    "tomatoes":     "TOMATOES",
    "potatoes":     "POTATOES",
    "barley":       "BARLEY",
    "oats":         "OATS",
    "sorghum":      "SORGHUM",
    "strawberries": "STRAWBERRIES",
    "avocado":      "AVOCADOS",
    "citrus":       "ORANGES",
    "walnuts":      "WALNUTS",
    "pistachios":   "PISTACHIOS",
    "canola":       "CANOLA",
    "sunflower":    "SUNFLOWER",
    "oranges":      "ORANGES",
    "lemons":       "LEMONS",
}

def _clean_yield_unit(raw: str) -> str:
    """Normalise NASS yield unit (e.g. 'BU / ACRE' → 'bu', 'TONS / ACRE' → 'ton')."""
    u = raw.lower().replace(" / acre", "").replace("/acre", "").strip()
    return "ton" if u == "tons" else u


def _fetch_nass_yield(crop: str) -> dict | None:
    """Fetch yield per acre from USDA NASS QuickStats API."""
    api_key = os.getenv("USDA_NASS_API_KEY", "")
    if not api_key:
        return None
    commodity = NASS_COMMODITY_MAP.get(crop)
    if not commodity:
        return None
    try:
        for year in range(datetime.now().year, datetime.now().year - 6, -1):
            r = requests.get(
                USDA_NASS_BASE,
                params={"key": api_key, "commodity_desc": commodity, "state_alpha": "CA",
                        "agg_level_desc": "STATE",
                        "statisticcat_desc": "YIELD", "year": str(year), "format": "JSON"},
                timeout=8,
            )
            if r.status_code != 200:
                continue
            for item in reversed(r.json().get("data", [])):
                val = item.get("Value", "").strip().replace(",", "")
                if val and val not in ("(D)", "(Z)", "(NA)"):
                    try:
                        return {"yield_per_acre": float(val),
                                "unit": _clean_yield_unit(item.get("unit_desc", "")),
                                "year": item.get("year", year), "source": "USDA NASS"}
                    except (ValueError, TypeError):
                        continue
        return None
    except Exception as e:
        print(f"    NASS yield error ({crop}): {e}")
        return None


def _fetch_nass(crop: str) -> dict | None:
    """Fetch price from USDA NASS QuickStats API."""
    api_key = os.getenv("USDA_NASS_API_KEY", "")
    if not api_key:
        return None
    commodity = NASS_COMMODITY_MAP.get(crop)
    if not commodity:
        return None
    try:
        for year in range(datetime.now().year, datetime.now().year - 6, -1):
            r = requests.get(
                USDA_NASS_BASE,
                params={"key": api_key, "commodity_desc": commodity, "state_alpha": "CA",
                        "agg_level_desc": "STATE",
                        "statisticcat_desc": "PRICE RECEIVED", "year": str(year), "format": "JSON"},
                timeout=8,
            )
            if r.status_code != 200:
                continue
            for item in reversed(r.json().get("data", [])):
                val = item.get("Value", "").strip().replace(",", "")
                if val and val not in ("(D)", "(Z)", "(NA)"):
                    try:
                        return {"price_per_unit_usd": float(val),
                                "unit": item.get("unit_desc", "unit").lower(),
                                "report_date": f"{item.get('year')}-{item.get('reference_period_desc', '')}",
                                "source": "USDA NASS"}
                    except (ValueError, TypeError):
                        continue
        return None
    except Exception as e:
        print(f"    NASS API error ({crop}): {e}")
        return None


# Conversion factors: (from_unit, to_unit) -> multiply_by
_UNIT_CONVERSIONS = {
    ("lb",  "cwt"):    0.01,
    ("cwt", "lb"):   100.0,
    ("lb",  "ton"):    0.0005,
    ("ton", "lb"):  2000.0,
    ("ton", "cwt"):   20.0,
    ("cwt", "ton"):    0.05,
}

def _clean_unit(raw: str) -> str:
    """Strip NASS '$ / ' prefix and normalise to lowercase."""
    return raw.lower().replace("$ / ", "").replace("$/", "").strip()


def _align_yield(yield_per_acre: float, yield_unit: str, price_unit: str) -> float:
    """Convert yield so its unit matches the price unit."""
    if yield_unit == price_unit:
        return yield_per_acre
    factor = _UNIT_CONVERSIONS.get((yield_unit, price_unit))
    return round(yield_per_acre * factor, 4) if factor else yield_per_acre


def fetch_live_prices(crop_categories: list) -> dict:
    """
    Require current NASS yield and price data for every crop; never substitute sample values.
    """
    live_prices = []
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")

    for crop in crop_categories:
        key = crop.lower().replace(" ", "_").replace("-", "_")

        print(f"  Fetching: {crop}")
        nass_yield = _fetch_nass_yield(key)
        if not nass_yield:
            raise ValueError(f"USDA NASS yield unavailable for {crop!r}; no value substituted")
        crop_info = {"yield_per_acre": nass_yield["yield_per_acre"], "unit": nass_yield["unit"]}
        print(f"    NASS yield: {nass_yield['yield_per_acre']} {nass_yield['unit']}/acre")

        price_data = _fetch_nass(key)

        if not price_data:
            raise ValueError(f"USDA NASS price unavailable for {crop!r}; no value substituted")

        # Clean unit string and align yield to price unit
        price_unit  = _clean_unit(price_data["unit"])
        yield_unit  = crop_info["unit"]
        yield_value = _align_yield(crop_info["yield_per_acre"], yield_unit, price_unit)

        if yield_unit != price_unit and (yield_unit, price_unit) not in _UNIT_CONVERSIONS:
            raise ValueError(f"Cannot align USDA yield unit {yield_unit!r} and price unit {price_unit!r} for {crop!r}")

        price_per_acre = round(price_data["price_per_unit_usd"] * yield_value, 2)
        print(
            f"    ${price_data['price_per_unit_usd']:.2f}/{price_unit} "
            f"x {yield_value} {price_unit}/acre "
            f"= ${price_per_acre:.2f}/acre  [{price_data['source']}]"
        )

        live_prices.append({
            "crop_category": crop,
            "price_per_unit_usd": price_data["price_per_unit_usd"],
            "unit": price_unit,
            "yield_per_acre": yield_value,
            "price_per_acre_usd": price_per_acre,
            "fetched_at": now,
            "usda_report_date": price_data.get("report_date", "N/A"),
            "source": price_data.get("source", "unknown"),
        })

    return {"live_prices": live_prices}
