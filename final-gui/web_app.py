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


def load_measurement_backend():
    spec = importlib.util.spec_from_file_location("measureapp_backend", BASE_DIR / "gui-v1.py")
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
    ],
    "complete_ac": [
        {"id": "cv", "label": "C-V", "x": "Vdc_pv", "y": "C", "dataset": "cv_curve", "yMin": 0, "filterBelowMin": True},
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
        self.stop_event = threading.Event()
        self.worker: Optional[threading.Thread] = None

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            variables = numeric_variables(self.datasets)
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
            }


STATE = RunState()


def terminal_log(message: str) -> None:
    print(f"[MeasureApp Web] {message}", flush=True)


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


def configured_defaults_section() -> Dict[str, Any]:
    config = load_default_settings_file()
    section = config.get("advanced_settings", config)
    if not isinstance(section, dict):
        raise ValueError("default_settings.yaml advanced_settings must be a mapping.")
    return section


def settings_with_config_defaults() -> Any:
    settings = backend.Settings()
    defaults = configured_defaults_section()
    for field in fields(settings):
        if field.name in defaults:
            value = coerce_value(defaults[field.name], getattr(settings, field.name))
            if field.name in {"dmm_addr", "lockin_i_addr", "lockin_v_addr", "fg_addr", "smu_addr"}:
                value = normalize_gpib_address(value)
            setattr(settings, field.name, value)
    return settings


def default_speed() -> str:
    defaults = configured_defaults_section()
    return str(defaults.get("test_speed", "Medium"))


def default_settings_dict() -> Dict[str, Any]:
    data = serialize_summary(asdict(settings_with_config_defaults()))
    data["test_speed"] = default_speed()
    return data


def settings_from_payload(payload: Dict[str, Any]) -> Any:
    settings_data = payload.get("settings", {})
    settings = settings_with_config_defaults()
    for field in fields(settings):
        if field.name in settings_data:
            value = coerce_value(settings_data[field.name], getattr(settings, field.name))
            if field.name in {"dmm_addr", "lockin_i_addr", "lockin_v_addr", "fg_addr", "smu_addr"}:
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
    return settings


def serialize_summary(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "__dataclass_fields__"):
        return {k: serialize_summary(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): serialize_summary(v) for k, v in value.items()}
    if isinstance(value, list):
        return [serialize_summary(v) for v in value]
    return value


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
        if settings.auto_smu_range and not existing_calibration:
            terminal_log("Automatic SMU range is enabled and no calibration exists. Calibrating before measurement...")
            with STATE.lock:
                STATE.mode = "smu_calibration"
            calibration_result = backend.MeasurementEngine(settings, terminal_log, STATE.stop_event).run_smu_range_calibration()
            calibration = serialize_summary(calibration_result.summary)
            apply_calibration_to_settings(settings, calibration)
            with STATE.lock:
                STATE.smu_calibration = calibration
                STATE.output_files.extend(str(path) for path in calibration_result.output_files)
            terminal_log("Automatic SMU range calibration complete. Continuing measurement.")
        elif settings.auto_smu_range and existing_calibration:
            apply_calibration_to_settings(settings, existing_calibration)

        with STATE.lock:
            STATE.mode = mode
        terminal_log(f"Starting {mode} measurement with {speed} speed.")
        engine = backend.MeasurementEngine(settings, terminal_log, STATE.stop_event, live_callback, live_control_getter)
        result = engine.run_selected(selected, speed)
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
            STATE.status = "failed"
            STATE.short_error = f"Stopped by safety limit: {exc}"
            STATE.completed_at = datetime.now().isoformat(timespec="seconds")
        terminal_log("Safety stop:\n" + traceback.format_exc())
    except Exception as exc:
        with STATE.lock:
            STATE.status = "failed"
            STATE.short_error = str(exc)
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
            STATE.short_error = str(exc)
            STATE.completed_at = datetime.now().isoformat(timespec="seconds")
        terminal_log("Calibration failed:\n" + traceback.format_exc())


@app.get("/")
def index():
    return render_template("index.html", default_settings=default_settings_dict(), default_plots=DEFAULT_PLOTS)


@app.get("/api/defaults")
def defaults():
    return jsonify({"settings": default_settings_dict(), "default_plots": DEFAULT_PLOTS})


@app.post("/api/start")
def start():
    payload = request.get_json(force=True)
    mode = payload.get("mode", "standard_dc")
    speed = payload.get("speed", "Medium")
    settings = settings_from_payload(payload)
    with STATE.lock:
        if STATE.status == "running":
            return jsonify({"ok": False, "error": "A measurement is already running."}), 409
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
        STATE.live_control = {
            "smu_voltage_v": settings.manual_smu_voltage_v,
            "fg_frequency_hz": settings.freq_start_hz,
        }
        STATE.stop_event = threading.Event()
        STATE.worker = threading.Thread(target=run_measurement, args=(payload,), daemon=True)
        STATE.worker.start()
    return jsonify({"ok": True})


@app.post("/api/calibrate-smu")
def calibrate_smu():
    payload = request.get_json(force=True)
    with STATE.lock:
        if STATE.status == "running":
            return jsonify({"ok": False, "error": "A measurement is already running."}), 409
        STATE.status = "running"
        STATE.mode = "smu_calibration"
        STATE.short_error = ""
        STATE.started_at = datetime.now().isoformat(timespec="seconds")
        STATE.completed_at = ""
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
    with STATE.lock:
        STATE.live_control.update(updates)
        current = dict(STATE.live_control)
    terminal_log(f"Live control updated: {updates}")
    return jsonify({"ok": True, "live_control": current})


@app.post("/api/stop")
def stop():
    STATE.stop_event.set()
    terminal_log("Stop requested from web UI. Waiting for safe shutdown.")
    return jsonify({"ok": True})


@app.get("/api/status")
def status():
    return jsonify(STATE.snapshot())


@app.get("/api/results")
def results():
    with STATE.lock:
        return jsonify({
            "datasets": STATE.datasets,
            "summary": STATE.summary,
            "status": STATE.status,
            "short_error": STATE.short_error,
        })


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
    with STATE.lock:
        STATE.status = "completed"
        STATE.mode = inferred_mode
        STATE.datasets = datasets
        STATE.output_files = [str(path.resolve())]
        STATE.combined_csv = path.resolve()
        STATE.short_error = ""
        STATE.completed_at = datetime.now().isoformat(timespec="seconds")
    terminal_log(f"Uploaded CSV loaded for plotting: {path} | inferred mode={inferred_mode}")
    return jsonify({"ok": True, "dataset": dataset_name, "mode": inferred_mode, "rows": len(rows)})


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
    terminal_log("Open browser: http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
