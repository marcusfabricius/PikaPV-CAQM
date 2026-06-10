from __future__ import annotations

import csv
import importlib.util
import logging
import math
import sys
import threading
import traceback
from dataclasses import asdict, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.utils import secure_filename

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SETTINGS_FILE = BASE_DIR / "default_settings.yaml"
SPEED_PROFILE_SETTINGS_FILE = BASE_DIR / "speedprofile_settings.yaml"
APP_STARTED_AT = datetime.now().isoformat(timespec="seconds")
ESTIMATE_FIXED_OVERHEAD_S = 4.0
ESTIMATE_PER_VOLTAGE_OVERHEAD_S = 0.10
ESTIMATE_AUTO_SMU_SEARCH_READS_PER_POINT = 4
ESTIMATE_AUTO_SMU_SEARCH_READ_S = 0.15
ESTIMATE_EXTRA_DC_SAMPLE_S = 0.10

SPEED_PROFILE_ORDER = ["Custom", "Fast", "Medium", "Slow"]
SPEED_PROFILE_KIND_ORDER = ["frequency_sweep", "cv_curve"]
SPEED_PROFILE_KIND_FILE_KEYS = {
    "frequency_sweep": "Frequency sweep",
    "cv_curve": "CV curve",
}
SPEED_PROFILE_KIND_ALIASES = {
    **{external: internal for internal, external in SPEED_PROFILE_KIND_FILE_KEYS.items()},
    **{internal: internal for internal in SPEED_PROFILE_KIND_FILE_KEYS},
    "frequency sweep": "frequency_sweep",
    "Frequency Sweep": "frequency_sweep",
    "CV Curve": "cv_curve",
    "C-V curve": "cv_curve",
    "C-V Curve": "cv_curve",
}


def speed_profile_block(
    vdc_step: float,
    points_per_decade: float,
    minimum_points: float,
    settle_smu: float,
    settle_freq: float,
    lockin_wait: float,
) -> Dict[str, float]:
    return {
        "vdc_pv_step_size_v": vdc_step,
        "frequency_points_per_decade": points_per_decade,
        "minimum_frequency_points": minimum_points,
        "settling_after_smu_s": settle_smu,
        "settling_after_freq_s": settle_freq,
        "lockin_time_constant_wait_s": lockin_wait,
    }


SPEED_PROFILE_DEFAULTS: Dict[str, Dict[str, Dict[str, float]]] = {
    "Custom": {
        "frequency_sweep": speed_profile_block(0.025, 8, 8, 1.0, 4.0, 0.0),
        "cv_curve": speed_profile_block(0.025, 8, 8, 1.0, 4.0, 0.0),
    },
    "Fast": {
        "frequency_sweep": speed_profile_block(0.05, 4, 6, 1.0, 2.6, 0.0),
        "cv_curve": speed_profile_block(0.05, 4, 6, 1.0, 2.6, 0.0),
    },
    "Medium": {
        "frequency_sweep": speed_profile_block(0.025, 8, 10, 1.0, 4.0, 0.0),
        "cv_curve": speed_profile_block(0.025, 8, 10, 1.0, 4.0, 0.0),
    },
    "Slow": {
        "frequency_sweep": speed_profile_block(0.01, 16, 16, 1.0, 5.6, 0.0),
        "cv_curve": speed_profile_block(0.01, 16, 16, 1.0, 5.6, 0.0),
    },
}

CUSTOM_SPEED_FIELD_TO_PROFILE_TARGET = {
    "custom_frequency_sweep_vdc_pv_step_size_v": ("frequency_sweep", "vdc_pv_step_size_v"),
    "custom_frequency_sweep_frequency_points_per_decade": ("frequency_sweep", "frequency_points_per_decade"),
    "custom_frequency_sweep_minimum_frequency_points": ("frequency_sweep", "minimum_frequency_points"),
    "custom_frequency_sweep_settling_after_smu_s": ("frequency_sweep", "settling_after_smu_s"),
    "custom_frequency_sweep_settling_after_freq_s": ("frequency_sweep", "settling_after_freq_s"),
    "custom_frequency_sweep_lockin_time_constant_wait_s": ("frequency_sweep", "lockin_time_constant_wait_s"),
    "custom_cv_vdc_pv_step_size_v": ("cv_curve", "vdc_pv_step_size_v"),
    "custom_cv_frequency_points_per_decade": ("cv_curve", "frequency_points_per_decade"),
    "custom_cv_minimum_frequency_points": ("cv_curve", "minimum_frequency_points"),
    "custom_cv_settling_after_smu_s": ("cv_curve", "settling_after_smu_s"),
    "custom_cv_settling_after_freq_s": ("cv_curve", "settling_after_freq_s"),
    "custom_cv_lockin_time_constant_wait_s": ("cv_curve", "lockin_time_constant_wait_s"),
}

LEGACY_CUSTOM_SPEED_FIELD_TO_PROFILE_TARGET = {
    "custom_vdc_pv_step_size_v": ("frequency_sweep", "vdc_pv_step_size_v"),
    "custom_frequency_points_per_decade": ("frequency_sweep", "frequency_points_per_decade"),
    "custom_minimum_frequency_points": ("frequency_sweep", "minimum_frequency_points"),
    "settling_after_smu_s": ("frequency_sweep", "settling_after_smu_s"),
    "settling_after_freq_s": ("frequency_sweep", "settling_after_freq_s"),
    "lockin_time_constant_wait_s": ("frequency_sweep", "lockin_time_constant_wait_s"),
}

SPEED_PROFILE_FILE_KEYS = {
    "vdc_pv_step_size_v": "Vdc_pv Step Size",
    "frequency_points_per_decade": "Frequency points per decade",
    "minimum_frequency_points": "Minimum frequency points",
    "settling_after_smu_s": "Settling SMU change time",
    "settling_after_freq_s": "Settling FG change time",
    "lockin_time_constant_wait_s": "Lockin Time wait",
}

SPEED_PROFILE_KEY_ALIASES = {
    **{external: internal for internal, external in SPEED_PROFILE_FILE_KEYS.items()},
    **{internal: internal for internal in SPEED_PROFILE_FILE_KEYS},
}


def load_measurement_backend():
    spec = importlib.util.spec_from_file_location("pikapv_backend", BASE_DIR / "gui-v1.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load gui-v1.py measurement backend.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


backend = load_measurement_backend()
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024
logging.getLogger("werkzeug").setLevel(logging.WARNING)


MODE_TO_SELECTION = {
    "standard_dc": {"iv_plot": True, "pv_plot": True},
    "frequency_sweep": {
        "z_real_plot": True,
        "z_imag_plot": True,
        "z_mag_plot": True,
        "z_phase_plot": True,
        "nyquist_plot": True,
        "cap_freq_plot": True,
    },
    "complete_ac": {"cv_plot": True},
    "live_lockin": {"ab_live": True},
}

DEFAULT_PLOTS = {
    "standard_dc": [
        {"id": "iv", "label": "I-V", "x": "Vdc_pv", "y": "Idc_pv", "dataset": "iv_pv_sweep", "xMin": 0, "yMin": 0, "filterBelowMin": True},
        {"id": "pv", "label": "P-V", "x": "Vdc_pv", "y": "Pdc_pv", "dataset": "iv_pv_sweep", "xMin": 0, "yMin": 0, "filterBelowMin": True},
    ],
    "frequency_sweep": [
        {"id": "zreal_freq", "label": "Z_real over frequency", "x": "frequency", "y": "Z_real", "dataset": "frequency_sweep", "xScale": "log"},
        {"id": "zimag_freq", "label": "Z_imag over frequency", "x": "frequency", "y": "Z_imag", "dataset": "frequency_sweep", "xScale": "log"},
        {"id": "zmag_freq", "label": "Z_mag over frequency", "x": "frequency", "y": "Z_mag", "dataset": "frequency_sweep", "xScale": "log", "yMin": 0},
        {"id": "phase_freq", "label": "Phase_Z over frequency", "x": "frequency", "y": "Phase_Z", "dataset": "frequency_sweep", "xScale": "log"},
        {"id": "nyquist", "label": "Nyquist plot", "x": "Z_real", "y": "Z_imag", "dataset": "frequency_sweep", "xLabel": "Z_real [ohm]", "yLabel": "Z_imag [ohm]", "nyquist": True},
    ],
    "complete_ac": [
        {"id": "cv", "label": "C-V", "x": "Vdc_pv", "y": "C", "dataset": "cv_curve", "yMin": 0, "filterBelowMin": True},
        {"id": "cv_per_area", "label": "C-V over Area", "x": "Vdc_pv", "y": "C", "dataset": "cv_curve", "xLabel": "Vdc_pv [V]", "yLabel": "C / Area [F/cm\u00b2]", "yMin": 0, "filterBelowMin": True, "needsArea": True, "perArea": True},
        {"id": "cf_at_vdc", "label": "C over frequency at Vdc_pv", "x": "frequency", "y": "C", "dataset": "cv_frequency_sweeps", "xScale": "log", "yMin": 0, "filterBelowMin": True, "needsTargetVdc": True},
    ],
    "live_lockin": [
        {"id": "lockin_value", "label": "Lock-in value over time", "x": "time_s", "y": "lockin12_corrected_Vpv_Vrms", "dataset": "ab_live"},
        {"id": "lockin_phase", "label": "Lock-in phase over time", "x": "time_s", "y": "lockin12_corrected_phase_deg", "dataset": "ab_live"},
    ],
}


class RunState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.status = "idle"
        self.mode = ""
        self.speed = "Medium"
        self.short_error = ""
        self.started_at = ""
        self.completed_at = ""
        self.datasets: Dict[str, List[Dict[str, Any]]] = {}
        self.summary: Dict[str, Any] = {}
        self.output_files: List[str] = []
        self.combined_csv: Optional[Path] = None
        self.live_rows: List[Dict[str, Any]] = []
        self.live_control: Dict[str, Any] = {}
        self.smu_calibration: Dict[str, Any] = {}
        self.measurement_options: Dict[str, Any] = {}
        self.progress: Dict[str, Any] = {}
        self.stop_event = threading.Event()
        self.worker: Optional[threading.Thread] = None

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            variables = numeric_variables(self.datasets)
            progress = dict(self.progress)
            if self.status == "running" and progress:
                started = safe_datetime_parse(str(progress.get("started_at") or self.started_at))
                if started is not None:
                    elapsed_s = max(0.0, (datetime.now() - started).total_seconds())
                    progress["elapsed_s"] = elapsed_s
                    base_percent = float(progress.get("base_percent", 0.0))
                    end_percent = float(progress.get("end_percent", 100.0))
                    if progress.get("estimated_total_s"):
                        estimated_total_s = max(1.0, float(progress["estimated_total_s"]))
                        span = max(0.0, end_percent - base_percent)
                        progress["percent"] = min(end_percent - 0.2, base_percent + elapsed_s / estimated_total_s * span)
                        progress["remaining_s"] = max(0.0, estimated_total_s - elapsed_s)
                    elif progress.get("indeterminate"):
                        span = max(0.0, end_percent - base_percent)
                        progress["percent"] = min(end_percent, base_percent + span * (1.0 - math.exp(-elapsed_s / 8.0)))
            elif self.status == "completed" and progress:
                progress["percent"] = 100.0
                progress["remaining_s"] = 0.0
            return {
                "status": self.status,
                "mode": self.mode,
                "speed": self.speed,
                "short_error": self.short_error,
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "datasets": {name: len(rows) for name, rows in self.datasets.items()},
                "variables": variables,
                "output_files": self.output_files,
                "combined_csv": str(self.combined_csv) if self.combined_csv else "",
                "live_rows": self.live_rows[-300:],
                "live_control": dict(self.live_control),
                "smu_calibration": dict(self.smu_calibration),
                "measurement_options": dict(self.measurement_options),
                "progress": progress,
            }


STATE = RunState()
LED_CONFIG_LOCK = threading.Lock()
SPEED_PROFILE_LOCK = threading.Lock()


def terminal_log(message: str) -> None:
    print(f"[PikaPV Web] {message}", flush=True)


def configure_led_generator(settings: Any, source: str, raise_errors: bool = True) -> bool:
    with LED_CONFIG_LOCK:
        session = backend.VisaController(settings, terminal_log)
        try:
            terminal_log(
                f"{source}: configuring LED generator at {settings.led_fg_addr} "
                f"with duty={settings.led_duty_cycle_percent:.3g}%."
            )
            session.open(
                need_dmm=False,
                need_lockin_i=False,
                need_lockin_v=False,
                need_fg=False,
                need_smu=False,
                need_led_fg=True,
            )
            session.configure_led_fg()
            return True
        except Exception as exc:
            message = user_facing_error(exc)
            terminal_log(f"{source}: LED generator configuration failed: {message}")
            if raise_errors:
                raise RuntimeError(message) from exc
            return False
        finally:
            session.close()


def user_facing_error(exc: BaseException) -> str:
    text = str(exc)
    lower = text.lower()
    if any(marker in lower for marker in ["vi_error_tmo", "timeout", "not responding", "failed to query", "rejected"]):
        return (
            f"{text} Check if all devices are turned on, booted correctly, connected to GPIB, "
            "and the solar cell is connected."
        )
    return text


def load_default_settings_file() -> Dict[str, Any]:
    if not DEFAULT_SETTINGS_FILE.exists():
        return {}
    with DEFAULT_SETTINGS_FILE.open("r", encoding="utf-8") as handle:
        if yaml is None:
            return parse_simple_yaml_mapping(handle.read())
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError("default_settings.yaml must contain a YAML mapping at the top level.")
    return loaded


def parse_simple_yaml_scalar(value: str) -> Any:
    value = value.strip()
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.lower() in {"null", "none", "~"}:
        return None
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    try:
        if any(ch in value for ch in [".", "e", "E"]):
            return float(value)
        return int(value)
    except ValueError:
        return value


def parse_simple_yaml_mapping(text: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    current_section: Optional[Dict[str, Any]] = None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith(" ") and line.endswith(":"):
            section_name = line[:-1].strip()
            result[section_name] = {}
            current_section = result[section_name]
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        target = current_section if raw_line.startswith(" ") and current_section is not None else result
        target[key.strip()] = parse_simple_yaml_scalar(value)
    return result


def safe_float(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except Exception:
        return None
    if not math.isfinite(f):
        return None
    return f


def safe_datetime_parse(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def numeric_variables(datasets: Dict[str, List[Dict[str, Any]]]) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    for name, rows in datasets.items():
        cols: List[str] = []
        seen = set()
        for row in rows[:100]:
            for key, value in row.items():
                if key in seen:
                    continue
                if safe_float(value) is not None:
                    seen.add(key)
                    cols.append(key)
        result[name] = cols
    return result


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "on"}


def coerce_value(raw: Any, current: Any) -> Any:
    if isinstance(current, bool):
        return parse_bool(raw)
    if isinstance(current, Path):
        return Path(str(raw)).expanduser()
    if isinstance(current, int) and not isinstance(current, bool):
        return int(float(raw))
    if isinstance(current, float):
        return float(raw)
    return str(raw)


def normalize_gpib_address(value: Any) -> str:
    text = str(value).strip()
    if text.upper().startswith("GPIB"):
        return text
    return f"GPIB0::{text}::INSTR"


def default_speed_profiles() -> Dict[str, Dict[str, Dict[str, float]]]:
    return {
        name: {
            kind: dict(SPEED_PROFILE_DEFAULTS[name][kind])
            for kind in SPEED_PROFILE_KIND_ORDER
        }
        for name in SPEED_PROFILE_ORDER
    }


def load_speed_profile_settings_file() -> Dict[str, Any]:
    if not SPEED_PROFILE_SETTINGS_FILE.exists():
        return {}
    with SPEED_PROFILE_SETTINGS_FILE.open("r", encoding="utf-8") as handle:
        if yaml is None:
            return parse_simple_speed_profile_settings(handle.read())
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError("speedprofile_settings.yaml must contain a YAML mapping at the top level.")
    return loaded


def parse_simple_speed_profile_settings(text: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {"speed_profiles": {}}
    current_profile: Optional[str] = None
    current_kind: Optional[str] = None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if indent == 0 and stripped == "speed_profiles:":
            continue
        if indent == 2 and stripped.endswith(":"):
            current_profile = stripped[:-1].strip()
            result["speed_profiles"][current_profile] = {}
            current_kind = None
            continue
        if indent == 4 and current_profile and stripped.endswith(":"):
            raw_kind = stripped[:-1].strip()
            current_kind = SPEED_PROFILE_KIND_ALIASES.get(raw_kind, raw_kind)
            result["speed_profiles"][current_profile][current_kind] = {}
            continue
        if indent >= 4 and current_profile and ":" in stripped:
            key, value = stripped.split(":", 1)
            target = result["speed_profiles"][current_profile]
            if current_kind and indent >= 6:
                target = target.setdefault(current_kind, {})
            target[key.strip()] = parse_simple_yaml_scalar(value)
    return result


def configured_speed_profiles() -> Dict[str, Dict[str, Dict[str, float]]]:
    profiles = default_speed_profiles()
    config = load_speed_profile_settings_file()
    section = config.get("speed_profiles", config)
    if not isinstance(section, dict):
        return profiles

    for profile_name in SPEED_PROFILE_ORDER:
        raw_profile = section.get(profile_name, {})
        if not isinstance(raw_profile, dict):
            continue
        for raw_key, raw_value in raw_profile.items():
            kind = SPEED_PROFILE_KIND_ALIASES.get(str(raw_key))
            if kind in SPEED_PROFILE_KIND_ORDER and isinstance(raw_value, dict):
                for raw_inner_key, raw_inner_value in raw_value.items():
                    internal_key = SPEED_PROFILE_KEY_ALIASES.get(str(raw_inner_key), str(raw_inner_key))
                    if internal_key not in SPEED_PROFILE_FILE_KEYS:
                        continue
                    value = safe_float(raw_inner_value)
                    if value is not None:
                        profiles[profile_name][kind][internal_key] = value
                continue

            # Backward compatibility for the old flat profile YAML: a flat value
            # applies to both measurement-specific profile blocks.
            internal_key = SPEED_PROFILE_KEY_ALIASES.get(str(raw_key), str(raw_key))
            if internal_key not in SPEED_PROFILE_FILE_KEYS:
                continue
            value = safe_float(raw_value)
            if value is not None:
                for kind_name in SPEED_PROFILE_KIND_ORDER:
                    profiles[profile_name][kind_name][internal_key] = value
    return profiles


def save_speed_profiles(profiles: Dict[str, Dict[str, Dict[str, float]]]) -> None:
    lines = [
        "# Speed profile defaults for web measurements.",
        "# Custom is edited from the Advanced settings panel.",
        "",
        "speed_profiles:",
    ]
    for profile_name in SPEED_PROFILE_ORDER:
        lines.append(f"  {profile_name}:")
        for kind in SPEED_PROFILE_KIND_ORDER:
            lines.append(f"    {SPEED_PROFILE_KIND_FILE_KEYS[kind]}:")
            profile = profiles.get(profile_name, {}).get(kind, SPEED_PROFILE_DEFAULTS[profile_name][kind])
            for internal_key, external_key in SPEED_PROFILE_FILE_KEYS.items():
                lines.append(f"      {external_key}: {float(profile[internal_key]):.12g}")
    with SPEED_PROFILE_LOCK:
        SPEED_PROFILE_SETTINGS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def profile_kind_for_mode(mode: str) -> str:
    return "cv_curve" if mode == "complete_ac" else "frequency_sweep"


def speed_profile_for(
    profiles: Dict[str, Dict[str, Dict[str, float]]],
    speed: str,
    kind: str,
) -> Dict[str, float]:
    profile_name = speed if speed in SPEED_PROFILE_ORDER else "Medium"
    kind_name = kind if kind in SPEED_PROFILE_KIND_ORDER else "frequency_sweep"
    return profiles.get(profile_name, {}).get(kind_name, SPEED_PROFILE_DEFAULTS[profile_name][kind_name])


def sync_backend_speed_profiles(
    profiles: Dict[str, Dict[str, Dict[str, float]]],
    kind: str = "frequency_sweep",
) -> None:
    for profile_name in SPEED_PROFILE_ORDER:
        profile = speed_profile_for(profiles, profile_name, kind)
        current = backend.SPEED_LEVELS.get(profile_name, backend.SPEED_LEVELS["Medium"])
        backend.AUTO_VDC_STEP_BY_SPEED[profile_name] = float(profile["vdc_pv_step_size_v"])
        backend.SPEED_LEVELS[profile_name] = backend.SpeedLevel(
            profile_name,
            points_per_decade=max(1, int(round(float(profile["frequency_points_per_decade"])))),
            minimum_frequency_points=max(1, int(round(float(profile["minimum_frequency_points"])))),
            repeats=current.repeats,
            settling_multiplier=1.0,
        )


def set_custom_profile_settings(settings: Any, kind: str, profile: Dict[str, float]) -> None:
    prefix = "custom_frequency_sweep" if kind == "frequency_sweep" else "custom_cv"
    setattr(settings, f"{prefix}_vdc_pv_step_size_v", float(profile["vdc_pv_step_size_v"]))
    setattr(settings, f"{prefix}_frequency_points_per_decade", int(round(float(profile["frequency_points_per_decade"]))))
    setattr(settings, f"{prefix}_minimum_frequency_points", int(round(float(profile["minimum_frequency_points"]))))
    setattr(settings, f"{prefix}_settling_after_smu_s", float(profile["settling_after_smu_s"]))
    setattr(settings, f"{prefix}_settling_after_freq_s", float(profile["settling_after_freq_s"]))
    setattr(settings, f"{prefix}_lockin_time_constant_wait_s", float(profile["lockin_time_constant_wait_s"]))


def apply_effective_profile_to_settings(settings: Any, profile: Dict[str, float]) -> None:
    settings.settling_after_smu_s = float(profile["settling_after_smu_s"])
    settings.settling_after_freq_s = float(profile["settling_after_freq_s"])
    settings.lockin_time_constant_wait_s = float(profile["lockin_time_constant_wait_s"])
    settings.custom_vdc_pv_step_size_v = float(profile["vdc_pv_step_size_v"])
    settings.custom_frequency_points_per_decade = int(round(float(profile["frequency_points_per_decade"])))
    settings.custom_minimum_frequency_points = int(round(float(profile["minimum_frequency_points"])))


def apply_custom_profile_to_settings(settings: Any, profiles: Dict[str, Dict[str, Dict[str, float]]]) -> None:
    for kind in SPEED_PROFILE_KIND_ORDER:
        set_custom_profile_settings(settings, kind, speed_profile_for(profiles, "Custom", kind))
    apply_effective_profile_to_settings(settings, speed_profile_for(profiles, "Custom", "frequency_sweep"))


def custom_profile_from_settings(settings: Any, kind: str) -> Dict[str, float]:
    prefix = "custom_frequency_sweep" if kind == "frequency_sweep" else "custom_cv"
    fallback = speed_profile_block(
        float(getattr(settings, "custom_vdc_pv_step_size_v", 0.025)),
        float(getattr(settings, "custom_frequency_points_per_decade", 8)),
        float(getattr(settings, "custom_minimum_frequency_points", 8)),
        float(getattr(settings, "settling_after_smu_s", 1.0)),
        float(getattr(settings, "settling_after_freq_s", 4.0)),
        float(getattr(settings, "lockin_time_constant_wait_s", 0.0)),
    )
    return {
        "vdc_pv_step_size_v": float(getattr(settings, f"{prefix}_vdc_pv_step_size_v", fallback["vdc_pv_step_size_v"])),
        "frequency_points_per_decade": float(getattr(settings, f"{prefix}_frequency_points_per_decade", fallback["frequency_points_per_decade"])),
        "minimum_frequency_points": float(getattr(settings, f"{prefix}_minimum_frequency_points", fallback["minimum_frequency_points"])),
        "settling_after_smu_s": float(getattr(settings, f"{prefix}_settling_after_smu_s", fallback["settling_after_smu_s"])),
        "settling_after_freq_s": float(getattr(settings, f"{prefix}_settling_after_freq_s", fallback["settling_after_freq_s"])),
        "lockin_time_constant_wait_s": float(getattr(settings, f"{prefix}_lockin_time_constant_wait_s", fallback["lockin_time_constant_wait_s"])),
    }


def apply_speed_profile_to_settings(
    settings: Any,
    speed: str,
    profiles: Dict[str, Dict[str, Dict[str, float]]],
    mode: str,
) -> None:
    kind = profile_kind_for_mode(mode)
    profile = custom_profile_from_settings(settings, kind) if speed == "Custom" else speed_profile_for(profiles, speed, kind)
    apply_effective_profile_to_settings(settings, profile)
    backend.AUTO_VDC_STEP_BY_SPEED[speed] = float(profile["vdc_pv_step_size_v"])
    current = backend.SPEED_LEVELS.get(speed, backend.SPEED_LEVELS["Medium"])
    backend.SPEED_LEVELS[speed] = backend.SpeedLevel(
        speed,
        points_per_decade=max(1, int(round(float(profile["frequency_points_per_decade"])))),
        minimum_frequency_points=max(1, int(round(float(profile["minimum_frequency_points"])))),
        repeats=current.repeats,
        settling_multiplier=1.0,
    )


def configured_defaults_section() -> Dict[str, Any]:
    config = load_default_settings_file()
    section = config.get("advanced_settings", config)
    if not isinstance(section, dict):
        raise ValueError("default_settings.yaml advanced_settings must be a mapping.")
    return section


def settings_with_config_defaults() -> Any:
    settings = backend.Settings()
    defaults = configured_defaults_section()
    speed_profiles = configured_speed_profiles()
    sync_backend_speed_profiles(speed_profiles)
    for field in fields(settings):
        if field.name in defaults:
            value = coerce_value(defaults[field.name], getattr(settings, field.name))
            if field.name in {"dmm_addr", "lockin_i_addr", "lockin_v_addr", "fg_addr", "led_fg_addr", "smu_addr"}:
                value = normalize_gpib_address(value)
            setattr(settings, field.name, value)
    apply_custom_profile_to_settings(settings, speed_profiles)
    return settings


def default_speed() -> str:
    defaults = configured_defaults_section()
    return str(defaults.get("test_speed", "Medium"))


def default_settings_dict() -> Dict[str, Any]:
    data = serialize_summary(asdict(settings_with_config_defaults()))
    data["test_speed"] = default_speed()
    return data


def speed_profiles_dict() -> Dict[str, Dict[str, Dict[str, float]]]:
    profiles = configured_speed_profiles()
    sync_backend_speed_profiles(profiles)
    return profiles


def settings_from_payload(payload: Dict[str, Any]) -> Any:
    settings_data = payload.get("settings", {})
    settings = settings_with_config_defaults()
    speed_profiles = configured_speed_profiles()
    sync_backend_speed_profiles(speed_profiles)
    for field in fields(settings):
        if field.name in settings_data:
            value = coerce_value(settings_data[field.name], getattr(settings, field.name))
            if field.name in {"dmm_addr", "lockin_i_addr", "lockin_v_addr", "fg_addr", "led_fg_addr", "smu_addr"}:
                value = normalize_gpib_address(value)
            setattr(settings, field.name, value)

    mode = payload.get("mode", "standard_dc")
    if mode == "frequency_sweep":
        frequency = payload.get("frequency", {})
        if frequency.get("operating_point") == "manual":
            settings.operating_point_mode = "MANUAL_SMU_VOLTAGE"
        else:
            settings.operating_point_mode = "MPP_SEARCH"
        if "manual_smu_voltage_v" in frequency:
            settings.manual_smu_voltage_v = float(frequency["manual_smu_voltage_v"])
        if "freq_start_hz" in frequency:
            settings.freq_start_hz = float(frequency["freq_start_hz"])
        if "freq_stop_hz" in frequency:
            settings.freq_stop_hz = float(frequency["freq_stop_hz"])

    if mode == "complete_ac":
        ac = payload.get("complete_ac", {})
        if "freq_start_hz" in ac:
            settings.freq_start_hz = float(ac["freq_start_hz"])
        if "freq_stop_hz" in ac:
            settings.freq_stop_hz = float(ac["freq_stop_hz"])
        if ac.get("frequency_mode") == "single":
            f = float(ac.get("single_frequency_hz") or settings.freq_start_hz)
            settings.freq_start_hz = f
            settings.freq_stop_hz = f
        if "cv_smu_step_v" in ac:
            settings.cv_smu_step_v = float(ac["cv_smu_step_v"])
    speed = str(payload.get("speed") or settings_data.get("test_speed") or default_speed())
    apply_speed_profile_to_settings(settings, speed, speed_profiles, mode)
    return settings


def count_linear_points(start: float, stop: float, step: float) -> int:
    if step <= 0 or stop < start:
        return 1
    return max(1, int(math.floor((stop - start) / step + 1e-12)) + 1)


def count_freq_points(settings: Any, speed: str) -> int:
    if math.isclose(settings.freq_start_hz, settings.freq_stop_hz, rel_tol=0.0, abs_tol=1e-12):
        return 1
    level = backend.SPEED_LEVELS.get(speed, backend.SPEED_LEVELS["Medium"])
    return len(backend.logspace_points(
        settings.freq_start_hz,
        settings.freq_stop_hz,
        level.points_per_decade,
        level.minimum_frequency_points,
    ))


def calibration_vdc_span(calibration: Optional[Dict[str, Any]], fallback_span: float) -> float:
    if not calibration:
        return fallback_span
    positive = calibration.get("positive_vdc_row", {})
    negative = calibration.get("negative_idc_row", {})
    start_vdc = safe_float(positive.get("Vdc_pv_V") if isinstance(positive, dict) else None)
    stop_vdc = safe_float(negative.get("Vdc_pv_V") if isinstance(negative, dict) else None)
    if start_vdc is None or stop_vdc is None:
        return fallback_span
    span = abs(stop_vdc - start_vdc)
    return span if span > 0 else fallback_span


def estimate_voltage_plan(settings: Any, speed: str, step_key: str, calibration: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cached = []
    if getattr(settings, "auto_smu_range", False) and getattr(settings, "auto_smu_step_by_speed", False):
        cache_key = (
            speed,
            round(float(settings.smu_start_v), 6),
            round(float(settings.smu_stop_v), 6),
            round(float(backend.AUTO_VDC_STEP_BY_SPEED.get(speed, backend.AUTO_VDC_STEP_BY_SPEED["Medium"])), 6),
        )
        cached = list(getattr(backend, "AUTO_SMU_SWEEP_CACHE", {}).get(cache_key, []))
        target_step = float(backend.AUTO_VDC_STEP_BY_SPEED.get(speed, backend.AUTO_VDC_STEP_BY_SPEED["Medium"]))
        fallback_span = max(min(float(settings.target_vpv_v), float(settings.max_vdc_pv_v)), target_step)
        usable_vdc_span = max(calibration_vdc_span(calibration, fallback_span), target_step)
        estimated_points = max(2, int(math.ceil(usable_vdc_span / target_step)) + 1)
        cached_points = len(cached)
        missing_points = max(0, estimated_points - cached_points)
        dc_repeat_overhead_s = max(0, int(getattr(settings, "dc_read_repeats", 1)) - 1) * ESTIMATE_EXTRA_DC_SAMPLE_S
        return {
            "points": max(1, cached_points, estimated_points),
            "auto_step": True,
            "cached_points": cached_points,
            "missing_points": missing_points,
            "search_overhead_s": missing_points * ESTIMATE_AUTO_SMU_SEARCH_READS_PER_POINT * (ESTIMATE_AUTO_SMU_SEARCH_READ_S + dc_repeat_overhead_s),
        }
    return {
        "points": count_linear_points(settings.smu_start_v, settings.smu_stop_v, float(getattr(settings, step_key))),
        "auto_step": False,
        "cached_points": 0,
        "missing_points": 0,
        "search_overhead_s": 0.0,
    }


def auto_smu_mpp_cache_ready(settings: Any, speed: str) -> bool:
    target_step = float(backend.AUTO_VDC_STEP_BY_SPEED.get(speed, backend.AUTO_VDC_STEP_BY_SPEED["Medium"]))
    cache_key = (
        speed,
        round(float(settings.smu_start_v), 6),
        round(float(settings.smu_stop_v), 6),
        round(target_step, 6),
    )
    return cache_key in getattr(backend, "AUTO_SMU_SWEEP_MPP_READY_CACHE", set())


def estimate_voltage_points(settings: Any, speed: str, step_key: str, calibration: Optional[Dict[str, Any]] = None) -> int:
    return int(estimate_voltage_plan(settings, speed, step_key, calibration)["points"])


def estimate_measurement_progress(payload: Dict[str, Any], settings: Any, calibration: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    mode = payload.get("mode", "standard_dc")
    speed = payload.get("speed", "Medium")
    settling_smu = max(0.0, float(settings.settling_after_smu_s))
    settling_freq = max(0.0, float(settings.settling_after_freq_s))
    lockin_wait = max(0.0, float(settings.lockin_time_constant_wait_s))
    dc_repeat_overhead_s = max(0, int(getattr(settings, "dc_read_repeats", 1)) - 1) * ESTIMATE_EXTRA_DC_SAMPLE_S
    voltage_read_overhead_s = ESTIMATE_PER_VOLTAGE_OVERHEAD_S + dc_repeat_overhead_s
    per_freq_s = settling_freq + lockin_wait + 0.15 + dc_repeat_overhead_s
    n_freq = count_freq_points(settings, speed)
    n_voltage = 1
    total_s = 1.0
    voltage_plan = {"auto_step": False, "cached_points": 0, "missing_points": 0, "search_overhead_s": 0.0}
    overhead_s = ESTIMATE_FIXED_OVERHEAD_S

    if mode == "standard_dc":
        voltage_plan = estimate_voltage_plan(settings, speed, "smu_step_v", calibration)
        n_voltage = int(voltage_plan["points"])
        total_s = n_voltage * (settling_smu + voltage_read_overhead_s) + float(voltage_plan["search_overhead_s"]) + overhead_s
    elif mode == "frequency_sweep":
        if (
            settings.operating_point_mode == "MPP_SEARCH"
            and getattr(settings, "auto_smu_range", False)
            and getattr(settings, "auto_smu_step_by_speed", False)
            and auto_smu_mpp_cache_ready(settings, speed)
        ):
            n_voltage = 1
            voltage_plan = {
                "auto_step": True,
                "cached_points": 1,
                "missing_points": 0,
                "search_overhead_s": 0.0,
                "mpp_cache_reused": True,
            }
        elif settings.operating_point_mode == "MPP_SEARCH":
            voltage_plan = estimate_voltage_plan(settings, speed, "smu_step_v", calibration)
            n_voltage = int(voltage_plan["points"])
        else:
            n_voltage = 1
        total_s = n_voltage * (settling_smu + voltage_read_overhead_s) + float(voltage_plan["search_overhead_s"]) + n_freq * per_freq_s + overhead_s
    elif mode == "complete_ac":
        voltage_plan = estimate_voltage_plan(settings, speed, "cv_smu_step_v", calibration)
        n_voltage = int(voltage_plan["points"])
        total_s = n_voltage * (settling_smu + voltage_read_overhead_s + n_freq * per_freq_s) + float(voltage_plan["search_overhead_s"]) + overhead_s
    elif mode == "live_lockin":
        return {}
    elif mode == "smu_calibration":
        n_voltage = count_linear_points(settings.smu_start_v, settings.smu_stop_v, 0.005)
        total_s = n_voltage * 0.15

    return {
        "mode": mode,
        "label": mode.replace("_", " "),
        "estimated_total_s": max(1.0, total_s),
        "estimated_voltage_points": n_voltage,
        "estimated_frequency_points": n_freq if mode in {"frequency_sweep", "complete_ac"} else 0,
        "estimated_overhead_s": overhead_s,
        "auto_smu_step_cached_points": int(voltage_plan.get("cached_points", 0)),
        "auto_smu_step_missing_points": int(voltage_plan.get("missing_points", 0)),
        "auto_smu_step_search_overhead_s": float(voltage_plan.get("search_overhead_s", 0.0)),
        "base_percent": float(payload.get("progress_base_percent", 0.0)),
        "end_percent": float(payload.get("progress_end_percent", 100.0)),
        "percent": 0.0,
        "elapsed_s": 0.0,
        "remaining_s": max(1.0, total_s),
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }


def calibration_progress(base_percent: float, end_percent: float, label: str = "Calibrating solar cell") -> Dict[str, Any]:
    return {
        "mode": "smu_calibration",
        "label": label,
        "indeterminate": True,
        "hide_time": True,
        "base_percent": base_percent,
        "end_percent": end_percent,
        "percent": base_percent,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "message": "Calibrating solar cell before the timed estimate starts...",
    }


def serialize_summary(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "__dataclass_fields__"):
        return {k: serialize_summary(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): serialize_summary(v) for k, v in value.items()}
    if isinstance(value, list):
        return [serialize_summary(v) for v in value]
    return value


def json_safe(value: Any) -> Any:
    return serialize_summary(value)


def save_combined_csv(mode: str, speed: str, datasets: Dict[str, List[Dict[str, Any]]], output_dir: Path) -> Path:
    rows: List[Dict[str, Any]] = []
    run_timestamp = datetime.now().isoformat(timespec="seconds")
    for dataset_name, dataset_rows in datasets.items():
        for row in dataset_rows:
            combined = {
                "run_mode": mode,
                "speed_level": speed,
                "run_timestamp": run_timestamp,
                "dataset_name": dataset_name,
                **row,
            }
            if "frequency_hz" not in combined and "f_ac_Hz" in combined:
                combined["frequency_hz"] = combined["f_ac_Hz"]
            if "frequency" not in combined and "f_ac_Hz" in combined:
                combined["frequency"] = combined["f_ac_Hz"]
            if "Z_real" not in combined and "Z_real_ohm" in combined:
                combined["Z_real"] = combined["Z_real_ohm"]
            if "Z_imag" not in combined and "Z_imag_ohm" in combined:
                combined["Z_imag"] = combined["Z_imag_ohm"]
            if "Z_mag" not in combined:
                combined["Z_mag"] = combined.get("Z_magnitude_ohm", combined.get("Z_mag_ohm", ""))
            if "Rj" not in combined and "R_junction_ohm" in combined:
                combined["Rj"] = combined["R_junction_ohm"]
            if "Cj" not in combined and "C_junction_fit_F" in combined:
                combined["Cj"] = combined["C_junction_fit_F"]
            if "C_parallel" not in combined:
                combined["C_parallel"] = combined.get("C_parallel_median_F", combined.get("C_uncorrected_F", ""))
            if "Phase_Z" not in combined and "Z_phase_deg" in combined:
                combined["Phase_Z"] = combined["Z_phase_deg"]
            if "Vac_pv" not in combined and "Vac_mag_corrected_V" in combined:
                combined["Vac_pv"] = combined["Vac_mag_corrected_V"]
            if "Iac_pv" not in combined and "Iac_mag_corrected_A" in combined:
                combined["Iac_pv"] = combined["Iac_mag_corrected_A"]
            if "Phase_Vac" not in combined and "Vac_phase_corrected_deg" in combined:
                combined["Phase_Vac"] = combined["Vac_phase_corrected_deg"]
            if "Phase_Iac" not in combined and "Iac_phase_corrected_deg" in combined:
                combined["Phase_Iac"] = combined["Iac_phase_corrected_deg"]
            if "capacitance" not in combined:
                combined["capacitance"] = combined.get("C_final_median_F", combined.get("C_uncorrected_F", ""))
            if "C" not in combined:
                combined["C"] = combined.get("C_final_median_F", combined.get("C_uncorrected_F", ""))
            if "Vdc_pv" not in combined:
                combined["Vdc_pv"] = combined.get("Vdc_pv_V", combined.get("Vdc_pv_median_V", combined.get("Vdc_pv_mean_V", "")))
            if "Idc_pv" not in combined:
                combined["Idc_pv"] = combined.get("Idc_pv_A", combined.get("Idc_pv_median_A", ""))
            if "Power" not in combined and "Pdc_pv_W" in combined:
                combined["Power"] = combined["Pdc_pv_W"]
            if "Pdc_pv" not in combined:
                combined["Pdc_pv"] = combined.get("Pdc_pv_W", combined.get("Power", ""))
            if "SMU_V" not in combined and "smu_voltage_V" in combined:
                combined["SMU_V"] = combined["smu_voltage_V"]
            if "V_SMU" not in combined:
                combined["V_SMU"] = combined.get("smu_voltage_V", combined.get("SMU_V", combined.get("operating_point_smu_voltage_V", "")))
            if dataset_name == "frequency_sweep":
                combined["final_Vdc_pv"] = combined.get("operating_point_reference_Vdc_pv_V", combined.get("Vdc_pv", ""))
                combined["final_Idc_pv"] = combined.get("operating_point_reference_Idc_pv_A", combined.get("Idc_pv", ""))
                combined["final_Pdc_pv"] = combined.get("operating_point_reference_Pdc_pv_W", combined.get("Pdc_pv", ""))
                combined["final_V_SMU"] = combined.get("operating_point_smu_voltage_V", combined.get("V_SMU", ""))
            rows.append(combined)

    if not output_dir.is_absolute():
        output_dir = BASE_DIR / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"combined_{mode}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    backend.save_rows(rows, path)
    return path.resolve()


def infer_mode_from_rows(rows: List[Dict[str, Any]]) -> str:
    for row in rows:
        mode = str(row.get("run_mode", "")).strip()
        if mode in MODE_TO_SELECTION:
            return mode
        measurement_type = str(row.get("measurement_type", "")).strip().upper()
        if measurement_type == "CV":
            return "complete_ac"
        if measurement_type == "FREQUENCY_SWEEP":
            return "frequency_sweep"
    columns = set()
    for row in rows[:50]:
        columns.update(row.keys())
    if {"Vdc_pv_median_V", "C_final_median_F"} & columns:
        return "complete_ac"
    if {"Z_real_ohm", "Z_imag_ohm", "f_ac_Hz", "frequency_hz"} & columns:
        return "frequency_sweep"
    if {"lockin12_corrected_Vpv_Vrms", "lockin15_X_Vrms"} & columns:
        return "live_lockin"
    if {"Vdc_pv_V", "Idc_pv_A", "Pdc_pv_W", "Power"} & columns:
        return "standard_dc"
    return "standard_dc"


def datasets_from_uploaded_rows(rows: List[Dict[str, Any]], fallback_name: str) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        dataset_name = str(row.get("dataset_name", "")).strip()
        if dataset_name:
            grouped.setdefault(dataset_name, []).append(row)
    if grouped:
        return grouped

    mode = infer_mode_from_rows(rows)
    columns = set()
    for row in rows[:50]:
        columns.update(row.keys())
    if mode == "complete_ac" and {"Vdc_pv_median_V", "C_final_median_F"} & columns:
        return {"cv_curve": rows}
    if mode == "complete_ac":
        return {"cv_frequency_sweeps": rows}
    if mode == "frequency_sweep":
        return {"frequency_sweep": rows}
    if mode == "live_lockin":
        return {"ab_live": rows}
    if mode == "standard_dc":
        return {"iv_pv_sweep": rows}
    return {fallback_name: rows}


def add_derived_rj_to_complete_ac_datasets(
    datasets: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, List[Dict[str, Any]]]:
    detailed_rows = datasets.get("cv_frequency_sweeps", [])
    if not detailed_rows:
        return datasets

    def group_key(row: Dict[str, Any]) -> str:
        for key in ("sweep_index", "smu_voltage_V", "V_SMU", "SMU_V"):
            value = row.get(key)
            if value not in (None, ""):
                return f"{key}:{value}"
        return ""

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in detailed_rows:
        key = group_key(row)
        if key:
            grouped.setdefault(key, []).append(row)

    estimates: Dict[str, Dict[str, Any]] = {}
    circuit_fits: Dict[str, Dict[str, Any]] = {}
    for key, rows in grouped.items():
        normalized = []
        for row in rows:
            frequency = safe_float(row.get("f_ac_Hz", row.get("frequency_hz", row.get("frequency"))))
            z_real = safe_float(row.get("Z_real_ohm", row.get("Z_real")))
            z_imag = safe_float(row.get("Z_imag_ohm", row.get("Z_imag")))
            raw_capacitance = safe_float(row.get("C_uncorrected_F", row.get("C_parallel")))
            if frequency is not None and z_real is not None and z_imag is not None:
                normalized.append({
                    "f_ac_Hz": frequency,
                    "Z_real_ohm": z_real,
                    "Z_imag_ohm": z_imag,
                    "C_uncorrected_F": raw_capacitance,
                })
        estimate = backend.junction_resistance_from_rows(normalized)
        circuit_fit = backend.equivalent_circuit_fit_from_rows(normalized)
        if estimate is not None:
            estimates[key] = estimate
        if circuit_fit is not None:
            circuit_fits[key] = circuit_fit
        for row in rows:
            if estimate is not None:
                row.update(estimate)
                row["Rj"] = estimate["R_junction_ohm"]
            if circuit_fit is not None:
                row["R_junction_ohm"] = circuit_fit["R_junction_fit_ohm"]
                row["R_series_estimate_ohm"] = circuit_fit["R_series_fit_ohm"]
                row["Rj"] = circuit_fit["R_junction_fit_ohm"]

    for row in datasets.get("cv_curve", []):
        key = group_key(row)
        estimate = estimates.get(key)
        if estimate:
            row.update(estimate)
            row["Rj"] = estimate["R_junction_ohm"]
        circuit_fit = circuit_fits.get(key)
        if circuit_fit:
            previous_capacitance = safe_float(row.get("C_final_median_F"))
            if previous_capacitance is not None and safe_float(row.get("C_parallel_median_F")) is None:
                row["C_parallel_median_F"] = previous_capacitance
            row.update(circuit_fit)
            row["R_junction_ohm"] = circuit_fit["R_junction_fit_ohm"]
            row["R_series_estimate_ohm"] = circuit_fit["R_series_fit_ohm"]
            row["Rj"] = circuit_fit["R_junction_fit_ohm"]
            row["Cj"] = circuit_fit["C_junction_fit_F"]
            row["C_final_median_F"] = circuit_fit["C_junction_fit_F"]
            row["C_final_mean_F"] = circuit_fit["C_junction_fit_F"]
            row["C_final_std_F"] = float("nan")
            row["C_final_method"] = "equivalent_circuit_cnls"
    return datasets


def run_measurement(payload: Dict[str, Any]) -> None:
    mode = payload.get("mode", "standard_dc")
    speed = payload.get("speed", "Medium")
    settings = settings_from_payload(payload)
    selected = MODE_TO_SELECTION.get(mode, MODE_TO_SELECTION["standard_dc"])

    def live_callback(name: str, rows: List[Dict[str, Any]]) -> None:
        with STATE.lock:
            STATE.live_rows = rows[-300:]
            STATE.datasets[name] = rows

    def live_control_getter() -> Dict[str, Any]:
        with STATE.lock:
            return dict(STATE.live_control)

    try:
        with STATE.lock:
            existing_calibration = dict(STATE.smu_calibration)
        requires_smu_calibration = mode != "live_lockin"
        if requires_smu_calibration and settings.auto_smu_range and not existing_calibration:
            terminal_log("Automatic SMU range is enabled and no calibration exists. Calibrating before measurement...")
            with STATE.lock:
                STATE.mode = "smu_calibration"
                STATE.progress = calibration_progress(0.0, 10.0)
            calibration_result = backend.MeasurementEngine(settings, terminal_log, STATE.stop_event).run_smu_range_calibration()
            calibration = serialize_summary(calibration_result.summary)
            apply_calibration_to_settings(settings, calibration)
            with STATE.lock:
                STATE.smu_calibration = calibration
                STATE.output_files.extend(str(path) for path in calibration_result.output_files)
                STATE.progress = estimate_measurement_progress({
                    **payload,
                    "progress_base_percent": 10.0,
                    "progress_end_percent": 100.0,
                }, settings, calibration)
            terminal_log("Automatic SMU range calibration complete. Continuing measurement.")
        elif requires_smu_calibration and settings.auto_smu_range and existing_calibration:
            apply_calibration_to_settings(settings, existing_calibration)

        with STATE.lock:
            STATE.mode = mode
        terminal_log(f"Starting {mode} measurement with {speed} speed.")
        engine = backend.MeasurementEngine(settings, terminal_log, STATE.stop_event, live_callback, live_control_getter)
        result = engine.run_selected(selected, speed)
        if mode == "complete_ac":
            add_derived_rj_to_complete_ac_datasets(result.datasets)
        combined = save_combined_csv(mode, speed, result.datasets, settings.output_dir)
        with STATE.lock:
            STATE.status = "completed"
            STATE.datasets.update(result.datasets)
            STATE.summary = serialize_summary(result.summary)
            STATE.output_files = [str(path) for path in result.output_files]
            STATE.combined_csv = combined
            STATE.completed_at = datetime.now().isoformat(timespec="seconds")
        terminal_log(f"Completed {mode}. Combined CSV: {combined}")
    except backend.UserStop as exc:
        with STATE.lock:
            STATE.status = "stopped"
            STATE.short_error = str(exc)
            STATE.completed_at = datetime.now().isoformat(timespec="seconds")
        terminal_log(str(exc))
    except backend.StopMeasurement as exc:
        with STATE.lock:
            STATE.status = "completed"
            STATE.short_error = f"Stopped by safety limit: {user_facing_error(exc)}"
            STATE.completed_at = datetime.now().isoformat(timespec="seconds")
        terminal_log("Safety stop:\n" + traceback.format_exc())
    except Exception as exc:
        with STATE.lock:
            STATE.status = "failed"
            STATE.short_error = user_facing_error(exc)
            STATE.completed_at = datetime.now().isoformat(timespec="seconds")
        terminal_log("Measurement failed:\n" + traceback.format_exc())


def apply_calibration_to_settings(settings: Any, calibration: Dict[str, Any]) -> None:
    for key in ["smu_start_v", "smu_stop_v"]:
        if key in calibration:
            setattr(settings, key, float(calibration[key]))


def run_calibration(payload: Dict[str, Any]) -> None:
    settings = settings_from_payload(payload)
    try:
        terminal_log("Manual SMU range calibration requested.")
        result = backend.MeasurementEngine(settings, terminal_log, STATE.stop_event).run_smu_range_calibration()
        calibration = serialize_summary(result.summary)
        with STATE.lock:
            STATE.status = "idle"
            STATE.mode = ""
            STATE.short_error = ""
            STATE.smu_calibration = calibration
            STATE.output_files = [str(path) for path in result.output_files]
            STATE.completed_at = datetime.now().isoformat(timespec="seconds")
        terminal_log("Manual SMU range calibration complete.")
    except backend.UserStop as exc:
        with STATE.lock:
            STATE.status = "stopped"
            STATE.short_error = str(exc)
            STATE.completed_at = datetime.now().isoformat(timespec="seconds")
        terminal_log(str(exc))
    except Exception as exc:
        with STATE.lock:
            STATE.status = "failed"
            STATE.short_error = user_facing_error(exc)
            STATE.completed_at = datetime.now().isoformat(timespec="seconds")
        terminal_log("Calibration failed:\n" + traceback.format_exc())


@app.get("/")
def index():
    return render_template(
        "index.html",
        default_settings=default_settings_dict(),
        default_plots=DEFAULT_PLOTS,
        speed_profiles=speed_profiles_dict(),
        app_started_at=APP_STARTED_AT,
        current_year=datetime.now().year,
    )


@app.get("/api/defaults")
def defaults():
    return jsonify({"settings": default_settings_dict(), "default_plots": DEFAULT_PLOTS, "speed_profiles": speed_profiles_dict()})


@app.post("/api/start")
def start():
    payload = request.get_json(force=True)
    mode = payload.get("mode", "standard_dc")
    speed = payload.get("speed", "Medium")
    settings = settings_from_payload(payload)
    with STATE.lock:
        if STATE.status == "running":
            return jsonify({"ok": False, "error": "A measurement is already running."}), 409
        existing_calibration = dict(STATE.smu_calibration)
        needs_calibration = bool(mode != "live_lockin" and settings.auto_smu_range and not existing_calibration)
        if mode != "live_lockin" and settings.auto_smu_range and existing_calibration:
            apply_calibration_to_settings(settings, existing_calibration)
        STATE.status = "running"
        STATE.mode = mode
        STATE.speed = speed
        STATE.short_error = ""
        STATE.started_at = datetime.now().isoformat(timespec="seconds")
        STATE.completed_at = ""
        STATE.datasets = {}
        STATE.summary = {}
        STATE.output_files = []
        STATE.combined_csv = None
        STATE.live_rows = []
        STATE.measurement_options = {
            "ac_frequency_mode": payload.get("complete_ac", {}).get("frequency_mode", "range"),
            "freq_start_hz": settings.freq_start_hz,
            "freq_stop_hz": settings.freq_stop_hz,
        }
        STATE.progress = calibration_progress(0.0, 10.0) if needs_calibration else estimate_measurement_progress(payload, settings, existing_calibration)
        STATE.live_control = {
            "smu_voltage_v": settings.manual_smu_voltage_v,
            "fg_frequency_hz": settings.freq_start_hz,
            "led_duty_cycle_percent": settings.led_duty_cycle_percent,
        }
        STATE.stop_event = threading.Event()
        STATE.worker = threading.Thread(target=run_measurement, args=(payload,), daemon=True)
        STATE.worker.start()
    return jsonify({"ok": True})


@app.post("/api/calibrate-smu")
def calibrate_smu():
    payload = request.get_json(force=True)
    settings = settings_from_payload(payload)
    with STATE.lock:
        if STATE.status == "running":
            return jsonify({"ok": False, "error": "A measurement is already running."}), 409
        STATE.status = "running"
        STATE.mode = "smu_calibration"
        STATE.short_error = ""
        STATE.started_at = datetime.now().isoformat(timespec="seconds")
        STATE.completed_at = ""
        STATE.progress = calibration_progress(0.0, 100.0, "Calibrating solar cell")
        STATE.stop_event = threading.Event()
        STATE.worker = threading.Thread(target=run_calibration, args=(payload,), daemon=True)
        STATE.worker.start()
    return jsonify({"ok": True})


@app.post("/api/live/control")
def live_control():
    payload = request.get_json(force=True)
    updates: Dict[str, Any] = {}
    if "smu_voltage_v" in payload:
        updates["smu_voltage_v"] = float(payload["smu_voltage_v"])
    if "fg_frequency_hz" in payload:
        updates["fg_frequency_hz"] = float(payload["fg_frequency_hz"])
    if "led_duty_cycle_percent" in payload:
        updates["led_duty_cycle_percent"] = min(99.0, max(1.0, float(payload["led_duty_cycle_percent"])))
    with STATE.lock:
        STATE.live_control.update(updates)
        current = dict(STATE.live_control)
    terminal_log(f"Live control updated: {updates}")
    return jsonify({"ok": True, "live_control": current})


@app.post("/api/led/control")
def led_control():
    payload = request.get_json(force=True)
    settings = settings_from_payload(payload)
    if "led_duty_cycle_percent" in payload:
        settings.led_duty_cycle_percent = float(payload["led_duty_cycle_percent"])
    settings.led_duty_cycle_percent = min(99.0, max(1.0, float(settings.led_duty_cycle_percent)))
    with STATE.lock:
        if STATE.status == "running" and STATE.mode == "live_lockin":
            STATE.live_control["led_duty_cycle_percent"] = settings.led_duty_cycle_percent
            terminal_log(f"Live LED brightness update queued: {settings.led_duty_cycle_percent:.3g}%")
            return jsonify({"ok": True, "led_duty_cycle_percent": settings.led_duty_cycle_percent, "queued": True})
    try:
        configure_led_generator(settings, "Advanced LED setting")
    except Exception as exc:
        return jsonify({"ok": False, "error": user_facing_error(exc)}), 500
    return jsonify({"ok": True, "led_duty_cycle_percent": settings.led_duty_cycle_percent})


@app.post("/api/speed-profiles/custom")
def save_custom_speed_profile():
    payload = request.get_json(force=True)
    settings_data = payload.get("settings", payload)
    profiles = configured_speed_profiles()
    custom = {
        kind: dict(speed_profile_for(profiles, "Custom", kind))
        for kind in SPEED_PROFILE_KIND_ORDER
    }
    mappings = dict(CUSTOM_SPEED_FIELD_TO_PROFILE_TARGET)
    if not any(settings_key in settings_data for settings_key in mappings):
        mappings.update(LEGACY_CUSTOM_SPEED_FIELD_TO_PROFILE_TARGET)
    for settings_key, (kind, profile_key) in mappings.items():
        if settings_key not in settings_data:
            continue
        value = safe_float(settings_data[settings_key])
        if value is not None:
            if profile_key == "vdc_pv_step_size_v" and value <= 0:
                return jsonify({"ok": False, "error": "Custom Vdc_pv Step Size must be positive."}), 400
            if profile_key in {"frequency_points_per_decade", "minimum_frequency_points"} and value <= 0:
                return jsonify({"ok": False, "error": "Custom frequency point settings must be positive."}), 400
            if profile_key != "vdc_pv_step_size_v" and value < 0:
                return jsonify({"ok": False, "error": "Custom settling times cannot be negative."}), 400
            if profile_key in {"frequency_points_per_decade", "minimum_frequency_points"}:
                custom[kind][profile_key] = max(1, int(round(value)))
            else:
                custom[kind][profile_key] = value
    profiles["Custom"] = custom
    save_speed_profiles(profiles)
    sync_backend_speed_profiles(profiles)
    return jsonify({"ok": True, "speed_profiles": profiles})


@app.post("/api/stop")
def stop():
    STATE.stop_event.set()
    terminal_log("Stop requested from web UI. Waiting for safe shutdown.")
    return jsonify({"ok": True})


@app.get("/api/status")
def status():
    return jsonify(json_safe(STATE.snapshot()))


@app.get("/api/results")
def results():
    with STATE.lock:
        return jsonify(json_safe({
            "datasets": STATE.datasets,
            "summary": STATE.summary,
            "status": STATE.status,
            "short_error": STATE.short_error,
        }))


@app.post("/api/upload")
def upload():
    uploaded = request.files.get("csv_file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"ok": False, "error": "Choose a CSV file first."}), 400
    upload_dir = BASE_DIR / "measurement_output" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / secure_filename(uploaded.filename)
    uploaded.save(path)
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    dataset_name = path.stem
    inferred_mode = infer_mode_from_rows(rows)
    datasets = datasets_from_uploaded_rows(rows, dataset_name)
    if inferred_mode == "complete_ac":
        add_derived_rj_to_complete_ac_datasets(datasets)
    uploaded_frequencies = {
        value
        for row in rows
        for value in [safe_float(row.get("f_ac_Hz", row.get("frequency_hz")))]
        if value is not None
    }
    uploaded_frequency_mode = "single" if inferred_mode == "complete_ac" and len(uploaded_frequencies) == 1 else "range"
    with STATE.lock:
        STATE.status = "completed"
        STATE.mode = inferred_mode
        STATE.measurement_options = {"ac_frequency_mode": uploaded_frequency_mode}
        STATE.datasets = datasets
        STATE.output_files = [str(path.resolve())]
        STATE.combined_csv = path.resolve()
        STATE.short_error = ""
        STATE.completed_at = datetime.now().isoformat(timespec="seconds")
    terminal_log(f"Uploaded CSV loaded for plotting: {path} | inferred mode={inferred_mode}")
    return jsonify({
        "ok": True,
        "dataset": dataset_name,
        "mode": inferred_mode,
        "frequency_mode": uploaded_frequency_mode,
        "rows": len(rows),
    })


@app.get("/download/combined")
def download_combined():
    with STATE.lock:
        path = STATE.combined_csv
        output_files = list(STATE.output_files)
    if path and not path.is_absolute():
        candidates = [BASE_DIR / path, Path.cwd() / path, path.resolve()]
        path = next((candidate for candidate in candidates if candidate.exists()), path)
    if (not path or not path.exists()) and output_files:
        for raw in output_files:
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidates = [BASE_DIR / candidate, Path.cwd() / candidate, candidate.resolve()]
                candidate = next((item for item in candidates if item.exists()), candidate)
            if candidate.exists() and candidate.suffix.lower() == ".csv":
                path = candidate
                break
    if not path or not path.exists():
        return jsonify({"ok": False, "error": "No combined CSV is available yet."}), 404
    return send_file(path, as_attachment=True)


if __name__ == "__main__":
    terminal_log(f"Script: {Path(__file__).resolve()}")
    terminal_log(f"Working directory: {Path.cwd()}")
    configure_led_generator(settings_with_config_defaults(), "Startup", raise_errors=False)
    terminal_log("Open browser: http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
