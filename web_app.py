from __future__ import annotations

import csv
import importlib.util
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


BASE_DIR = Path(__file__).resolve().parent


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
        {"id": "iv", "label": "I-V", "x": "Vdc_pv_V", "y": "Idc_pv_A", "dataset": "iv_pv_sweep"},
        {"id": "pv", "label": "P-V", "x": "Vdc_pv_V", "y": "Pdc_pv_W", "dataset": "iv_pv_sweep"},
    ],
    "frequency_sweep": [
        {"id": "zmag_freq", "label": "Z magnitude vs frequency", "x": "f_ac_Hz", "y": "Z_magnitude_ohm", "dataset": "frequency_sweep", "xScale": "log"},
        {"id": "zreal_freq", "label": "Z real vs frequency", "x": "f_ac_Hz", "y": "Z_real_ohm", "dataset": "frequency_sweep", "xScale": "log"},
        {"id": "zimag_freq", "label": "Z imaginary vs frequency", "x": "f_ac_Hz", "y": "Z_imag_ohm", "dataset": "frequency_sweep", "xScale": "log"},
        {"id": "phase_freq", "label": "Phase vs frequency", "x": "f_ac_Hz", "y": "Z_phase_deg", "dataset": "frequency_sweep", "xScale": "log"},
        {"id": "cap_freq", "label": "Capacitance vs frequency", "x": "f_ac_Hz", "y": "C_uncorrected_F", "dataset": "frequency_sweep", "xScale": "log"},
        {"id": "nyquist", "label": "Nyquist plot", "x": "Z_real_ohm", "y": "neg_Z_imag_ohm", "dataset": "frequency_sweep"},
    ],
    "complete_ac": [
        {"id": "cv", "label": "C-V", "x": "Vdc_pv_median_V", "y": "C_final_median_F", "dataset": "cv_curve"},
        {"id": "cf", "label": "C-f at selected voltage", "x": "f_ac_Hz", "y": "C_uncorrected_F", "dataset": "cv_frequency_sweeps", "xScale": "log"},
        {"id": "zmag_freq", "label": "Z magnitude vs frequency", "x": "f_ac_Hz", "y": "Z_magnitude_ohm", "dataset": "cv_frequency_sweeps", "xScale": "log"},
        {"id": "phase_freq", "label": "Phase vs frequency", "x": "f_ac_Hz", "y": "Z_phase_deg", "dataset": "cv_frequency_sweeps", "xScale": "log"},
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
            }


STATE = RunState()


def default_settings_dict() -> Dict[str, Any]:
    return serialize_summary(asdict(backend.Settings()))


def terminal_log(message: str) -> None:
    print(f"[MeasureApp Web] {message}", flush=True)


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


def settings_from_payload(payload: Dict[str, Any]) -> Any:
    settings_data = payload.get("settings", {})
    settings = backend.Settings()
    for field in fields(settings):
        if field.name in settings_data:
            setattr(settings, field.name, coerce_value(settings_data[field.name], getattr(settings, field.name)))

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
            settings.freq_stop_hz = f * 1.01
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
            if "capacitance" not in combined:
                combined["capacitance"] = combined.get("C_final_median_F", combined.get("C_uncorrected_F", ""))
            if "Power" not in combined and "Pdc_pv_W" in combined:
                combined["Power"] = combined["Pdc_pv_W"]
            if "SMU_V" not in combined and "smu_voltage_V" in combined:
                combined["SMU_V"] = combined["smu_voltage_V"]
            rows.append(combined)

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"combined_{mode}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    backend.save_rows(rows, path)
    return path


def run_measurement(payload: Dict[str, Any]) -> None:
    mode = payload.get("mode", "standard_dc")
    speed = payload.get("speed", "Medium")
    settings = settings_from_payload(payload)
    selected = MODE_TO_SELECTION.get(mode, MODE_TO_SELECTION["standard_dc"])

    def live_callback(name: str, rows: List[Dict[str, Any]]) -> None:
        with STATE.lock:
            STATE.live_rows = rows[-300:]
            STATE.datasets[name] = rows

    try:
        terminal_log(f"Starting {mode} measurement with {speed} speed.")
        engine = backend.MeasurementEngine(settings, terminal_log, STATE.stop_event, live_callback)
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
        STATE.stop_event = threading.Event()
        STATE.worker = threading.Thread(target=run_measurement, args=(payload,), daemon=True)
        STATE.worker.start()
    return jsonify({"ok": True})


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
    with STATE.lock:
        STATE.status = "completed"
        STATE.mode = "uploaded_csv"
        STATE.datasets = {dataset_name: rows}
        STATE.output_files = [str(path)]
        STATE.combined_csv = path
        STATE.short_error = ""
        STATE.completed_at = datetime.now().isoformat(timespec="seconds")
    terminal_log(f"Uploaded CSV loaded for plotting: {path}")
    return jsonify({"ok": True, "dataset": dataset_name, "rows": len(rows)})


@app.get("/download/combined")
def download_combined():
    with STATE.lock:
        path = STATE.combined_csv
    if not path or not path.exists():
        return jsonify({"ok": False, "error": "No combined CSV is available yet."}), 404
    return send_file(path, as_attachment=True)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
