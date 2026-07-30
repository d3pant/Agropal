"""Shared paths, IO, and data loaders for the ERPC agents.

Every ERPC agent (econ, policy, insurance, report) needs the same repo paths,
the same JSON reader, and the same view of the crop agent's latest output.
Keeping one copy here means the agents cannot silently disagree about which
crop file is current — a real bug this module was introduced to fix.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

# --- Paths -----------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
FORECASTER_DIR = REPO_ROOT / "forecaster"
OUTPUT_DIR = FORECASTER_DIR / "output"
CONFIG_DIR = FORECASTER_DIR / "config"
DATA_DIR = FORECASTER_DIR / "data"
FORMS_DIR = FORECASTER_DIR / "forms"

CROP_AGENT_DIR = REPO_ROOT / "crop_agent"
LIVESTOCK_DIR = REPO_ROOT / "Livestock"

FARM_CONFIG = CONFIG_DIR / "farm_config.json"
FARM_FIELDS_JSON = CROP_AGENT_DIR / "farm_fields.json"
LIVESTOCK_ERPC_MSG = LIVESTOCK_DIR / "erpc_message.json"
LIVESTOCK_STATUS = LIVESTOCK_DIR / "livestock_status.json"
STATUS_JSON = OUTPUT_DIR / "status.json"
ECON_REPORT = OUTPUT_DIR / "econ_report.json"
POLICY_REPORT = OUTPUT_DIR / "policy_report.json"
FILLED_PDF = OUTPUT_DIR / "ccc_576_filled.pdf"


def get_logger(name: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger(name)


logger = get_logger("erpc.common")


# --- JSON IO ---------------------------------------------------------------

def load_json(path: Path, default=None):
    """Read JSON, returning `default` (or {}) if the file is missing or invalid."""
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {} if default is None else default


def write_report(path: Path, data: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    logger.info("Wrote %s", path)
    return path


# --- Crop agent output -----------------------------------------------------

# The crop agent has written both naming schemes over time; `output_*.json` is
# the current one. Intermediate `*raw*` and `*erpc*` files are not full reports.
_CROP_GLOBS = ("output_*.json", "crop_agent_output_*.json")


def latest_crop_output() -> Optional[Path]:
    """Newest complete crop agent report by mtime, or None if none exist."""
    candidates = [
        p for pattern in _CROP_GLOBS for p in CROP_AGENT_DIR.glob(pattern)
        if "raw" not in p.name and "erpc" not in p.name
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def load_crop_output() -> tuple[dict, Optional[Path]]:
    """Load the latest crop report, normalized to task1–task4 keys.

    On-disk files use the raw `task1`..`task4` keys; some callers pass around a
    descriptive-key variant. Accept both so no agent depends on which was
    written. Returns ({} , None) when no crop output exists at all.
    """
    path = latest_crop_output()
    if path is None:
        return {}, None
    raw = load_json(path)
    if not raw:
        return {}, None
    return {
        "task4": raw.get("field_decisions") or raw.get("task4") or [],
        "task1": raw.get("fire_reduction") or raw.get("task1") or [],
        "task2": raw.get("economic_impact") or raw.get("task2") or {},
        "task3": raw.get("hydration_strategy") or raw.get("task3") or [],
    }, path
