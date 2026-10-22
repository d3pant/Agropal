"""Wildfire Agricultural Advisory System - Forecaster Agent.

Single-job runner: performs one check cycle and writes status.json.
If gate condition is met, also writes wake_up_packet.json.

Usage:
    python forecaster.py [--scenario no_fire|fire_threat] [--use-real-data]
    python forecaster.py --use-real-data  # requires .env with API keys
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("forecaster")

OUTPUT_DIR = Path(__file__).parent / "output"
CONFIG_DIR = Path(__file__).parent / "config"


# ---------------------------------------------------------------------------
# Threat level helpers
# ---------------------------------------------------------------------------

THREAT_ORDER = ["GREEN", "WATCH", "WARNING", "CRITICAL", "EMERGENCY"]


def _threat_index(level: str) -> int:
    return THREAT_ORDER.index(level) if level in THREAT_ORDER else 0


def _max_threat(*levels: str) -> str:
    return max(levels, key=_threat_index)


def _escalate(level: str) -> str:
    idx = _threat_index(level)
    return THREAT_ORDER[min(idx + 1, len(THREAT_ORDER) - 1)]


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Gate condition evaluation
# ---------------------------------------------------------------------------

def _fwi_threat(fwi: float) -> str:
    """FWI-only threat from fire-weather conditions.

    FWI alone cannot reach CRITICAL — that requires a nearby fire too.
    Extreme FWI (≥20) is CRITICAL on its own; below that it caps at WARNING.
    """
    if fwi >= 20:
        return "CRITICAL"
    if fwi >= 12:
        return "WARNING"
    if fwi >= 9:
        return "WATCH"
    if fwi >= 6:
        return "GREEN"
    return "GREEN"


def _fire_distance_threat(distance_km: float | None) -> str:
    if distance_km is None:
        return "GREEN"
    if distance_km <= 50:
        return "CRITICAL"
    if distance_km <= 100:
        return "WARNING"
    if distance_km <= 200:
        return "WATCH"
    return "GREEN"


def _ndvi_threat(anomaly: float) -> str:
    if anomaly <= -2.0:
        return "CRITICAL"
    if anomaly <= -1.5:
        return "WARNING"
    if anomaly <= -1.0:
        return "WATCH"
    return "GREEN"


def _convergence_met(fwi: float, distance_km: float | None, ndvi: float) -> bool:
    """True when three weak signals converge: FWI > 7, distance < 150 km, NDVI < -1σ."""
    fire_close = distance_km is not None and distance_km < 150
    return fwi > 7 and fire_close and ndvi < -1.0


def evaluate_gate_condition(fwi: float, fire: dict | None, ndvi: float, farm_config: dict) -> dict:
    """Threat level is driven solely by fire distance. FWI and NDVI are recorded
    as context but do not escalate the threat level."""
    floor = farm_config["hard_safety_floor"]

    distance_km = fire["distance_km"] if fire else None

    # Distance-only threat
    combined = _fire_distance_threat(distance_km)

    # Hard floor: fire within hard-floor distance always forces CRITICAL
    hard_floor_hit = distance_km is not None and distance_km <= floor["fire_distance_km"]
    if hard_floor_hit:
        combined = _max_threat(combined, "CRITICAL")

    # Gate met at WARNING or above
    gate_met = _threat_index(combined) >= _threat_index("WARNING")

    # Reason string (distance only)
    if distance_km is None:
        reason = "no active fires detected"
    elif hard_floor_hit:
        reason = f"fire {distance_km:.1f} km away — within hard floor ({floor['fire_distance_km']} km) → CRITICAL"
    else:
        reason = f"fire {distance_km:.1f} km away · FWI {fwi:.1f} (context only)"

    confidence = min(0.99, 0.5 + 0.1 * _threat_index(combined))
    convergence = False
    flagged = 1 if distance_km is not None and _threat_index(combined) >= _threat_index("WARNING") else 0

    return {
        "threat_level": combined,
        "threat_confidence": round(confidence, 2),
        "gate_condition_met": gate_met,
        "gate_condition_reason": reason,
        "hard_floor_hit": hard_floor_hit,
        "convergence": convergence,
        "flagged_signals": flagged,
        "component_levels": {"fire_distance_km": distance_km},
    }


# ---------------------------------------------------------------------------
# Time-to-impact computation
# ---------------------------------------------------------------------------

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _zone_center(polygon: dict) -> dict:
    coords = polygon["coordinates"][0]
    lats = [c[1] for c in coords]
    lons = [c[0] for c in coords]
    return {"lat": sum(lats) / len(lats), "lon": sum(lons) / len(lons)}


def compute_zone_impacts(zones: list, fire: dict, wifire: dict | None, weather: dict | None = None) -> list:
    """Estimate each zone's fire arrival from the spread model, retaining unknowns."""
    from models.spread_model import time_to_impact
    weather = weather or {}
    location = (fire or {}).get("location") or {}
    wind_from = weather.get("wind_direction_degrees")
    if wind_from is None:
        wind_from = weather.get("wind_direction_deg")

    results = []
    for zone in zones:
        center = _zone_center(zone["polygon"])
        estimate = None
        if location.get("lat") is not None and location.get("lon") is not None and wind_from is not None:
            estimate = time_to_impact(
                location["lat"], location["lon"], center["lat"], center["lon"],
                wind_from, weather.get("wind_speed_kmh", 0) or 0,
                weather.get("soil_moisture", 0.15) or 0.15, (fire or {}).get("frp_mw", 0) or 0,
            )
        hours = estimate.get("hours") if estimate else None
        threatened = estimate.get("threatened") if estimate else None

        if hours is None:
            zone_threat = "UNKNOWN"
        elif hours <= 12:
            zone_threat = "CRITICAL"
        elif hours <= 48:
            zone_threat = "WARNING"
        elif hours <= 168:
            zone_threat = "WATCH"
        else:
            zone_threat = "GREEN"

        results.append({
            "zone_id": zone["zone_id"],
            "name": zone["name"],
            "risk_polygon": zone["polygon"],
            "time_to_impact_hours": hours,
            "time_to_impact_uncertainty_hours": None,
            "threatened": threatened,
            "impact_model": (wifire or {}).get("source") if wifire else "simplified Rothermel direction/time estimate",
            "threat_level": zone_threat,
        })

    return sorted(results, key=lambda z: z["time_to_impact_hours"] if z["time_to_impact_hours"] is not None else float("inf"))


# ---------------------------------------------------------------------------
# Agent messages
# ---------------------------------------------------------------------------

def build_agent_messages(zone_impacts: list, fire: dict) -> dict:
    critical_zones = [z for z in zone_impacts if z["threat_level"] in ("CRITICAL", "EMERGENCY")]
    warning_zones = [z for z in zone_impacts if z["threat_level"] == "WARNING"]
    first_zone = zone_impacts[0] if zone_impacts else None

    crop_msg = "Monitor situation closely."
    livestock_msg = "Monitor situation closely."
    erpc_msg = "Standby."

    if critical_zones:
        names = ", ".join(z["name"] for z in critical_zones)
        hours = critical_zones[0]["time_to_impact_hours"]
        crop_msg = f"Harvest {names} immediately; fire impact in ~{hours}h; coordinate evacuation routes with Livestock"
        livestock_msg = f"Move high-value animals from {names} within {max(1, hours - 2)}h; confirm transport capacity"
        erpc_msg = "Initiate resource allocation: prioritize Crop harvest vs. Livestock movement ROI; activate insurance pre-notifications"
    elif warning_zones:
        names = ", ".join(z["name"] for z in warning_zones)
        hours = warning_zones[0]["time_to_impact_hours"]
        crop_msg = f"Prepare harvest operations for {names}; fire impact possible within {hours}h"
        livestock_msg = f"Pre-position transport for animals in {names}; ready to evacuate within {hours}h"
        erpc_msg = "Pre-stage resources; prepare insurance notifications; assess evacuation route availability"

    return {
        "crop_agent": crop_msg,
        "livestock_agent": livestock_msg,
        "erpc": erpc_msg,
    }


# ---------------------------------------------------------------------------
# Main Forecaster class
# ---------------------------------------------------------------------------

class Forecaster:
    def __init__(self, farm_config_path: str | Path):
        """Initialize with farm config. Credentials are loaded from .env."""
        self.farm_config = self._load_json(farm_config_path)
        self.stage = 1
        self.threat_level = "GREEN"
        self.gate_condition_met = False
        self.status: dict = {}
        self.wake_up_packet: dict = {}

        # Fetched data
        self._fwi: float | None = None
        self._fire: dict | None = None
        self._ndvi: float | None = None
        self._weather: dict | None = None
        self._wifire: dict | None = None
        self._pyrecast: dict | None = None
        self._gate_assessment: dict = {}
        self._spread_prediction: dict | None = None

    @staticmethod
    def _load_json(path: str | Path) -> dict:
        with open(path, "r") as f:
            return json.load(f)

    def run_single_cycle(self, mock_scenario: str | None = None, location: dict | None = None) -> tuple[dict, dict | None]:
        """Execute one check cycle. Returns (status, wake_up_packet or None).

        Args:
            mock_scenario: 'no_fire' | 'fire_threat' | None (real data)
            location:      {'lat': float, 'lon': float} — overrides farm_config location
        """
        if location:
            self.farm_config["location"] = {**self.farm_config["location"], **location}

        logger.info("=== Forecaster: starting single cycle (scenario=%s, location=%s) ===",
                    mock_scenario or "real", self.farm_config["location"])

        self.fetch_stage1_data(mock_scenario)
        self.evaluate_gate_condition()
        self._compute_spread_prediction()
        self.write_status_json()

        if self.gate_condition_met:
            self.activate_stage2(mock_scenario)
            self.write_wake_up_packet()

        logger.info("=== Forecaster complete. Threat level: %s | Gate met: %s ===", self.threat_level, self.gate_condition_met)
        return self.status, self.wake_up_packet if self.gate_condition_met else None

    def fetch_stage1_data(self, mock_scenario: str | None = None) -> None:
        """Fetch lightweight Stage 1 data."""
        if mock_scenario:
            self._fetch_mock_stage1(mock_scenario)
        else:
            self._fetch_real_stage1()

    def _fetch_mock_stage1(self, scenario: str) -> None:
        from mock_data import NO_FIRE, FIRE_THREAT
        data = NO_FIRE if scenario == "no_fire" else FIRE_THREAT
        self._fwi = data["fwi"]
        self._fire = data["fire"]
        self._ndvi = data["ndvi_anomaly"]
        # Always use real Open-Meteo weather even in mock mode
        farm_loc = self.farm_config["location"]
        try:
            from data_sources.open_meteo import fetch_weather as fetch_om_weather
            om = fetch_om_weather(farm_loc["lat"], farm_loc["lon"])
            self._weather = {
                "wind_speed_kmh":      om["wind_speed_kmh"],
                "wind_direction_degrees": om["wind_direction_deg"],
                "wind_gusts_kmh":      om["wind_gusts_kmh"],
                "temperature_c":       om["temperature_c"],
                "humidity_percent":    om["humidity_pct"],
                "soil_moisture":       om["soil_moisture"],
                "fetched_at":          om["fetched_at"],
            }
            logger.info("Real weather: wind %.1f km/h @ %d°, soil moisture %.3f",
                        om["wind_speed_kmh"], om["wind_direction_deg"], om["soil_moisture"])
        except Exception as e:
            logger.warning("Open-Meteo fetch failed, using mock weather: %s", e)
            self._weather = data["weather"]
        logger.info("Mock Stage 1: FWI=%.1f fire=%s NDVI=%.2f", self._fwi, self._fire["name"] if self._fire else "none", self._ndvi)

    def _fetch_real_stage1(self) -> None:
        from data_sources.sdge_fpi import fetch_fwi
        from data_sources.nasa_firms import fetch_active_fires
        from data_sources.nasa_ndvi import fetch_ndvi_anomaly
        from data_sources.weather import fetch_weather

        farm_loc = self.farm_config["location"]

        try:
            self._fwi = fetch_fwi()
        except RuntimeError as e:
            logger.error("FWI fetch failed: %s", e)
            raise

        try:
            self._fire = fetch_active_fires(farm_loc)
        except RuntimeError as e:
            logger.error("FIRMS fetch failed: %s", e)
            raise

        try:
            self._ndvi = fetch_ndvi_anomaly(farm_loc)
        except RuntimeError as e:
            logger.error("NDVI fetch failed: %s", e)
            raise

        try:
            self._weather = fetch_weather(farm_loc)
        except RuntimeError as e:
            logger.warning("Weather fetch failed (non-fatal): %s", e)
            self._weather = {}

    def evaluate_gate_condition(self) -> None:
        """Apply multi-signal gate condition logic."""
        assert self._fwi is not None
        assert self._ndvi is not None

        self._gate_assessment = evaluate_gate_condition(
            fwi=self._fwi,
            fire=self._fire,
            ndvi=self._ndvi,
            farm_config=self.farm_config,
        )
        self.threat_level = self._gate_assessment["threat_level"]
        self.gate_condition_met = self._gate_assessment["gate_condition_met"]
        logger.info("Gate assessment: %s (met=%s)", self.threat_level, self.gate_condition_met)

    def activate_stage2(self, mock_scenario: str | None = None) -> None:
        """Fetch Stage 2 detailed predictions."""
        logger.info("Stage 2 activated")
        if mock_scenario:
            self._fetch_mock_stage2()
        else:
            self._fetch_real_stage2()

    def _fetch_mock_stage2(self) -> None:
        from mock_data import WIFIRE_SPREAD, PYRECAST_QUEUED
        self._wifire = {**WIFIRE_SPREAD, "fetched_at": _utc_now()}
        self._pyrecast = {**PYRECAST_QUEUED, "request_sent_at": _utc_now()}

    def _fetch_real_stage2(self) -> None:
        from predictors.wifire_predictor import WIFIREPredictor
        from predictors.pyrecast_predictor import PyrecastPredictor

        farm_loc = self.farm_config["location"]
        zones = self.farm_config["affected_zones"]

        try:
            wifire = WIFIREPredictor(farm_loc)
            self._wifire = wifire.predict_spread(self._fire, zones)
        except RuntimeError as e:
            logger.error("WIFIRE fetch failed: %s", e)
            raise

        try:
            pyrecast = PyrecastPredictor()
            self._pyrecast = pyrecast.predict_spread_async(
                self._fire.get("current_perimeter", {}),
                simulation_hours=336,  # 14 days
                ensemble_members=200,
            )
        except RuntimeError as e:
            logger.error("Pyrecast submission failed: %s", e)
            raise

    def _compute_spread_prediction(self) -> None:
        """Compute 6h/12h/24h spread ellipses for the nearest fire using Open-Meteo wind."""
        if not self._fire:
            self._spread_prediction = None
            return

        from models.spread_model import (
            compute_ellipse, ellipse_to_geojson_polygon,
            time_to_impact, head_rate_of_spread
        )

        fire_lat = self._fire["location"]["lat"]
        fire_lon = self._fire["location"]["lon"]
        frp      = self._fire.get("frp_mw") or self._fire.get("frp") or 0.0

        # Fetch weather at fire location (Open-Meteo, no key needed)
        try:
            from data_sources.open_meteo import fetch_weather as fetch_om_weather
            om = fetch_om_weather(fire_lat, fire_lon)
            wind_kmh  = om["wind_speed_kmh"]
            wind_dir  = om["wind_direction_deg"]
            soil_m    = om["soil_moisture"]
            logger.info("Spread model: wind %.1f km/h @ %d°, soil %.3f, FRP %.1f MW",
                        wind_kmh, wind_dir, soil_m, frp)
        except Exception as e:
            logger.warning("Open-Meteo failed for spread model: %s — using stored weather", e)
            w = self._weather or {}
            wind_kmh = w.get("wind_speed_kmh", 15)
            wind_dir = w.get("wind_direction_degrees", 0)
            soil_m   = w.get("soil_moisture", 0.15)

        # Compute ellipses for 3 horizons
        horizons = {}
        for hours in [6, 12, 24]:
            ellipse = compute_ellipse(
                fire_lat=fire_lat, fire_lon=fire_lon,
                wind_direction_deg=wind_dir,
                wind_kmh=wind_kmh,
                soil_moisture=soil_m,
                frp_mw=frp,
                hours=hours,
            )
            horizons[f"{hours}h"] = {
                "polygon": ellipse_to_geojson_polygon(ellipse),
                "head_km": ellipse.head_km,
                "back_km": ellipse.back_km,
                "flank_km": ellipse.semi_minor_km,
                "center": {"lat": ellipse.center_lat, "lon": ellipse.center_lon},
            }

        # Time-to-impact at farm location
        farm_loc = self.farm_config["location"]
        impact = time_to_impact(
            fire_lat=fire_lat, fire_lon=fire_lon,
            target_lat=farm_loc["lat"], target_lon=farm_loc["lon"],
            wind_direction_deg=wind_dir,
            wind_kmh=wind_kmh,
            soil_moisture=soil_m,
            frp_mw=frp,
        )

        self._spread_prediction = {
            "model": "Rothermel (1972) simplified + Anderson (1983) ellipse",
            "computed_at": _utc_now(),
            "wind_speed_kmh": wind_kmh,
            "wind_direction_deg": wind_dir,
            "soil_moisture": soil_m,
            "head_ros_kmh": head_rate_of_spread(wind_kmh, soil_m, frp),
            "horizons": horizons,
            "time_to_farm": impact,
        }
        logger.info("Spread: head ROS %.3f km/h, farm threatened=%s, ETA=%s h",
                    self._spread_prediction["head_ros_kmh"],
                    impact["threatened"], impact.get("hours"))

    def write_status_json(self) -> None:
        """Write status.json."""
        ga = self._gate_assessment
        farm_thresh = self.farm_config["custom_thresholds"]
        floor = self.farm_config["hard_safety_floor"]
        weather = self._weather or {}

        # Determine update interval based on threat level
        update_intervals = {"GREEN": 720, "WATCH": 120, "WARNING": 30, "CRITICAL": 15, "EMERGENCY": 5}
        next_update = update_intervals.get(self.threat_level, 120)

        self.status = {
            "run_id": str(uuid.uuid4()),
            "farm_id": self.farm_config.get("farm_id"),
            "farm_lat": self.farm_config.get("location", {}).get("lat"),
            "farm_lon": self.farm_config.get("location", {}).get("lon"),
            "timestamp": _utc_now(),
            "stage": self.stage,
            "threat_level": self.threat_level,
            "threat_level_confidence": ga.get("threat_confidence"),
            "fwi_index": self._fwi,
            "fwi_threshold_farmer": farm_thresh["fwi_trigger"],
            "fwi_threshold_floor": floor["fwi_trigger"],
            "nearest_fire": self._fire,
            "nearest_fire_distance_threshold_farmer": farm_thresh["fire_distance_km"],
            "nearest_fire_distance_threshold_floor": floor["fire_distance_km"],
            "vegetation_ndvi_anomaly": self._ndvi,
            "vegetation_threshold_farmer": farm_thresh["vegetation_stress_sigma"],
            "vegetation_threshold_floor": -2.0,
            "wind_speed_kmh": weather.get("wind_speed_kmh"),
            "wind_direction_degrees": weather.get("wind_direction_degrees"),
            "temperature_c": weather.get("temperature_c"),
            "humidity_percent": weather.get("humidity_percent"),
            "gate_condition_met": self.gate_condition_met,
            "gate_condition_reason": ga.get("gate_condition_reason"),
            "multi_signal_convergence": ga.get("convergence", False),
            "next_update_minutes": next_update,
            "stage_transition_triggered": self.gate_condition_met,
            "spread_prediction": self._spread_prediction,
        }

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        path = OUTPUT_DIR / "status.json"
        with open(path, "w") as f:
            json.dump(self.status, f, indent=2, default=str)
        logger.info("Wrote %s", path)

    def write_wake_up_packet(self) -> None:
        """Write wake_up_packet.json."""
        zones = self.farm_config["affected_zones"]
        zone_impacts = compute_zone_impacts(zones, self._fire, self._wifire, self._weather)
        agent_messages = build_agent_messages(zone_impacts, self._fire)

        weather = self._weather or {}

        from livestock_packet import build_livestock_packet
        location = self.farm_config["location"]
        livestock_profile = {"farm_id": self.farm_config["farm_id"], "centroid": location}
        livestock_profile_path = Path(__file__).parent.parent / "Livestock" / "farm_profile.json"
        if livestock_profile_path.exists():
            try:
                with open(livestock_profile_path) as profile_file:
                    livestock_profile = json.load(profile_file)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Could not load livestock profile for packet: %s", exc)
        livestock_contract = build_livestock_packet(
            self.status,
            livestock_profile,
            self.farm_config,
            self.status.get("run_id") or str(uuid.uuid4()),
        )
        self.wake_up_packet = {
            **livestock_contract,
            "activation_timestamp": _utc_now(),
            "farm_id": self.farm_config["farm_id"],
            "threat_level": self.threat_level,
            "threat_confidence": self._gate_assessment.get("threat_confidence"),
            "affected_zones": zone_impacts,
            "fire_data": self._fire,
            "wifire_predictions": self._wifire,
            "pyrecast_predictions": self._pyrecast,
            "weather_forecast": {
                **weather,
                "forecast_valid_until": weather.get("forecast_valid_until"),
            },
            "messages_to_agents": agent_messages,
        }

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        path = OUTPUT_DIR / "wake_up_packet.json"
        with open(path, "w") as f:
            json.dump(self.wake_up_packet, f, indent=2, default=str)
        logger.info("Wrote %s", path)

    def _estimate_smoke_trajectory(self, zone_impacts: list) -> dict:
        """Estimate smoke trajectory from wind direction (opposite of fire spread)."""
        weather = self._weather or {}
        wind_dir = weather.get("wind_direction_degrees")
        smoke_dir = (wind_dir + 180) % 360 if wind_dir is not None else None

        affected = [z["zone_id"] for z in zone_impacts if z["threat_level"] in ("CRITICAL", "WARNING")]
        return {
            "direction_degrees": smoke_dir,
            "affected_zones": affected,
            "visibility_impact": "heavy smoke" if self.threat_level in ("CRITICAL", "EMERGENCY") else "moderate smoke",
        }


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Wildfire Forecaster Agent - single job runner")
    parser.add_argument(
        "--scenario",
        choices=["no_fire", "fire_threat"],
        default="fire_threat",
        help="Mock scenario to run (default: fire_threat)",
    )
    parser.add_argument(
        "--use-real-data",
        action="store_true",
        help="Use real API calls instead of mock data (requires .env with API keys)",
    )
    parser.add_argument("--lat", type=float, default=None, help="Override location latitude")
    parser.add_argument("--lon", type=float, default=None, help="Override location longitude")
    args = parser.parse_args()

    farm_config_path = CONFIG_DIR / "farm_config.json"

    if not farm_config_path.exists():
        logger.error("Farm config not found: %s", farm_config_path)
        sys.exit(1)

    if args.use_real_data:
        env_path = Path(__file__).parent / ".env"
        required_keys = ["NASA_FIRMS_API_KEY", "SDGE_FPI_API_KEY", "PYRECAST_API_KEY", "WIFIRE_API_KEY"]
        missing = [k for k in required_keys if not os.environ.get(k)]
        if missing:
            logger.error("Missing env vars: %s — copy .env.example to .env and fill in values", ", ".join(missing))
            sys.exit(1)

    forecaster = Forecaster(farm_config_path=farm_config_path)

    location = {"lat": args.lat, "lon": args.lon} if args.lat and args.lon else None
    mock_scenario = None if args.use_real_data else args.scenario
    status, wake_up_packet = forecaster.run_single_cycle(mock_scenario=mock_scenario, location=location)

    print("\n--- STATUS SUMMARY ---")
    print(f"  Threat Level  : {status['threat_level']}")
    print(f"  Confidence    : {status.get('threat_level_confidence')}")
    print(f"  FWI           : {status['fwi_index']}")
    print(f"  Nearest Fire  : {status['nearest_fire']['name'] if status['nearest_fire'] else 'none'} "
          f"({status['nearest_fire']['distance_km']} km)" if status['nearest_fire'] else "  Nearest Fire  : none")
    print(f"  NDVI Anomaly  : {status['vegetation_ndvi_anomaly']}")
    print(f"  Gate Met      : {status['gate_condition_met']}")
    print(f"  Reason        : {status['gate_condition_reason']}")

    print(f"\n  output/status.json written.")
    if wake_up_packet:
        print(f"  output/wake_up_packet.json written.")
        print("\n--- ZONE IMPACTS ---")
        for zone in wake_up_packet["affected_zones"]:
            print(f"  {zone['zone_id']} {zone['name']}: {zone['threat_level']} (~{zone['time_to_impact_hours']}h)")
        print("\n--- AGENT MESSAGES ---")
        for agent, msg in wake_up_packet["messages_to_agents"].items():
            print(f"  [{agent}] {msg}")
    else:
        print("  wake_up_packet.json NOT written (gate condition not met).")


if __name__ == "__main__":
    main()
