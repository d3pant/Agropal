"""Bounded NOAA HRRR near-surface smoke retrieval and normalization.

NOAA MASSDEN in the selected HRRR surface files is kg/m^3 at about 8 m AGL.
This module converts values to micrograms/m^3 and preserves model valid times.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import hashlib
import json
import os
import tempfile
import math
from typing import Any

import httpx

NOMADS_URL = "https://nomads.ncep.noaa.gov/cgi-bin/filter_hrrr_2d.pl"
NOAA_ATTRIBUTION = "NOAA HRRR model data via NOMADS; accessed by Reboot the Earth."
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024


def normalize_smoke_grid(values, latitudes, longitudes, units: str) -> dict:
    unit = (units or "").lower().replace(" ", "").replace("**", "")
    if unit not in {"kg/m3", "kgm-3", "kgm^-3", "kgm−3"}:
        raise ValueError(f"Unexpected HRRR MASSDEN units: {units!r}")
    def clean_grid(grid):
        return [[(float(v) * 1e9 if v is not None and math.isfinite(float(v)) and float(v) >= 0 else None)
                 for v in row] for row in grid]
    return {
        "latitudes": [[float(v) for v in row] for row in latitudes],
        "longitudes": [[float(v) for v in row] for row in longitudes],
        "concentration_ug_m3": clean_grid(values),
        "units": "ug/m3",
    }


def build_nomads_request(cycle: datetime, forecast_hour: int, bounds: dict) -> dict:
    cycle = cycle.astimezone(timezone.utc)
    date = cycle.strftime("%Y%m%d")
    hour = cycle.strftime("%H")
    return {
        "dir": f"/hrrr.{date}/conus",
        "file": f"hrrr.t{hour}z.wrfsfcf{forecast_hour:02d}.grib2",
        "var_MASSDEN": "on", "subregion": "on", "leftlon": bounds["min_lon"],
        "rightlon": bounds["max_lon"], "toplat": bounds["max_lat"],
        "bottomlat": bounds["min_lat"],
    }


def _read_grib(path: Path) -> dict:
    try:
        import xarray as xr
    except ImportError as exc:
        raise RuntimeError("HRRR reading requires the open-source xarray/cfgrib/ecCodes packages") from exc
    ds = xr.open_dataset(path, engine="cfgrib", backend_kwargs={"indexpath": ""})
    try:
        variable = next((ds[name] for name, item in ds.data_vars.items()
                         if name.lower() == "massden" or item.attrs.get("GRIB_shortName", "").lower() == "massden"), None)
        if variable is None:
            raise ValueError("NOMADS response did not contain the HRRR MASSDEN field")
        lat = ds.get("latitude")
        lon = ds.get("longitude")
        if lat is None or lon is None:
            raise ValueError("HRRR subset has no latitude/longitude grid coordinates")
        return normalize_smoke_grid(variable.values.tolist(), lat.values.tolist(), lon.values.tolist(),
                                    variable.attrs.get("units", ""))
    finally:
        ds.close()


def _cycle_for(start: datetime) -> datetime:
    hour = start.hour - start.hour % 6
    return start.replace(hour=hour, minute=0, second=0, microsecond=0)


def _cache_path(cache_dir: Path, cycle: datetime, lead: int, bounds: dict) -> Path:
    region = {key: round(float(bounds[key]), 4)
              for key in ("min_lat", "max_lat", "min_lon", "max_lon")}
    region_key = hashlib.sha256(json.dumps(region, sort_keys=True).encode()).hexdigest()[:16]
    return cache_dir / f"hrrr_{cycle:%Y%m%d%H}_f{lead:02d}_{region_key}.json"


def fetch_smoke_forecast(bounds: dict, start_utc: datetime, end_utc: datetime,
                         http_client: httpx.Client | None = None,
                         grib_reader=_read_grib,
                         cache_dir: Path | None = None) -> dict:
    """Fetch one regional grid per requested forecast hour, with a 100 MB run cap."""
    start = start_utc.astimezone(timezone.utc)
    end = end_utc.astimezone(timezone.utc)
    if end < start:
        raise ValueError("forecast end precedes start")
    cycle = _cycle_for(start)
    first_hour = max(0, int((start - cycle).total_seconds() // 3600))
    last_hour = int((end - cycle).total_seconds() // 3600 + 0.999999)
    if last_hour > 48:
        return {"status": "unavailable", "reason": "requested travel window exceeds HRRR forecast horizon", "source": "NOAA HRRR"}
    own_client = http_client is None
    client = http_client or httpx.Client(timeout=30.0, follow_redirects=True)
    if cache_dir is None:
        cache_dir = Path(os.getenv(
            "LIVESTOCK_SMOKE_CACHE_DIR",
            str(Path(__file__).resolve().parents[1] / "output" / "smoke_cache"),
        ))
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    layers = []
    total_bytes = 0
    try:
        with tempfile.TemporaryDirectory(prefix="livestock-hrrr-") as temp_dir:
            for lead in range(first_hour, last_hour + 1):
                cache_file = _cache_path(cache_dir, cycle, lead, bounds)
                if cache_file.exists():
                    try:
                        cached = json.loads(cache_file.read_text())
                        if cached.get("model_cycle") == cycle.isoformat().replace("+00:00", "Z"):
                            layers.append(cached["layer"])
                            continue
                    except (OSError, json.JSONDecodeError, KeyError, TypeError):
                        pass
                params = build_nomads_request(cycle, lead, bounds)
                chunks = []
                size = 0
                with client.stream("GET", NOMADS_URL, params=params, timeout=30.0) as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes(65536):
                        size += len(chunk)
                        if size + total_bytes > MAX_DOWNLOAD_BYTES:
                            return {"status": "unavailable", "reason": "HRRR subset download limit exceeded", "source": "NOAA HRRR"}
                        chunks.append(chunk)
                content = b"".join(chunks)
                if not content.startswith(b"GRIB"):
                    return {"status": "partial", "reason": f"NOAA HRRR field unavailable for forecast hour {lead}", "source": "NOAA HRRR", "layers": layers}
                total_bytes += len(content)
                grib_path = Path(temp_dir) / f"hrrr_f{lead:02}.grib2"
                grib_path.write_bytes(content)
                grid = grib_reader(grib_path)
                layer = {"valid_at": (cycle + timedelta(hours=lead)).isoformat().replace("+00:00", "Z"), **grid}
                layers.append(layer)
                cache_temp = cache_file.with_suffix(".tmp")
                cache_temp.write_text(json.dumps({
                    "model_cycle": cycle.isoformat().replace("+00:00", "Z"), "layer": layer,
                }))
                cache_temp.replace(cache_file)
    except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
        return {"status": "partial" if layers else "unavailable", "reason": str(exc), "source": "NOAA HRRR", "layers": layers}
    finally:
        if own_client:
            client.close()
    return {
        "status": "available", "source": "NOAA HRRR HRRR-Smoke", "model_cycle": cycle.isoformat().replace("+00:00", "Z"),
        "valid_from": layers[0]["valid_at"], "valid_until": layers[-1]["valid_at"],
        "spatial_resolution_km": 3, "height_agl_m": 8, "units": "ug/m3",
        "attribution": NOAA_ATTRIBUTION, "download_bytes": total_bytes, "layers": layers,
    }
