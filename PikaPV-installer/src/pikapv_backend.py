"""
PikaPV combined GUI

This file combines the IV/PV sweep, CV sweep, impedance frequency sweep,
and A-B differential live monitor into one GUI-driven measurement program.

Required packages:
    pip install pyvisa

Notes:
    - PyVISA needs the correct VISA backend installed on the measurement PC.
    - The default GPIB addresses and commands are taken from the uploaded scripts.
    - Use Simulation mode only for checking the GUI without instruments.
"""

from __future__ import annotations

import csv
import math
import re
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import pyvisa  # type: ignore
except Exception:  # pragma: no cover
    pyvisa = None
else:
    try:
        import pyvisa_py  # type: ignore
    except Exception:
        pyvisa_py = None


# ============================================================================
# General helpers
# ============================================================================

FLOAT_RE = re.compile(
    r"[-+]?(?:(?:\d+\.\d*)|(?:\.\d+)|(?:\d+))(?:[Ee][-+]?\d+)?"
)


class StopMeasurement(Exception):
    """Raised when a measurement must stop safely."""


class UserStop(Exception):
    """Raised when the Stop button was pressed."""


class NegativeCurrentEndpoint(Exception):
    """Signals that a sweep reached its normal negative-current endpoint."""

    def __init__(
        self,
        vdc: float,
        idc: float,
        idc_raw: float,
        frequency_hz: float,
        quality: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(
            f"Negative-current endpoint reached: Idc_pv={idc:.6e} A at "
            f"Vdc_pv={vdc:.6e} V and f={frequency_hz:.6g} Hz."
        )
        self.vdc = vdc
        self.idc = idc
        self.idc_raw = idc_raw
        self.frequency_hz = frequency_hz
        self.quality = dict(quality or {})

    def as_dict(self) -> Dict[str, Any]:
        return {
            "Vdc_pv_V": self.vdc,
            "Idc_pv_A": self.idc,
            "Idc_adc1_raw": self.idc_raw,
            "Pdc_pv_W": self.vdc * self.idc,
            "f_ac_Hz": self.frequency_hz,
            "measurement_endpoint": "negative_current",
            **self.quality,
        }


def clean_instrument_reply(raw: str) -> str:
    return raw.replace("\x00", "").replace("\r", "").replace("\n", "").strip()


def parse_float_reply(raw: str, label: str, cmd: str) -> float:
    cleaned = clean_instrument_reply(raw)
    try:
        value = float(cleaned)
    except ValueError:
        matches = FLOAT_RE.findall(cleaned)
        if not matches:
            raise ValueError(
                f"{label} returned raw={raw!r}, cleaned={cleaned!r} for {cmd!r}."
            )
        value = float(matches[-1])
    if not math.isfinite(value):
        raise ValueError(f"{label} returned non-finite value {value!r} for {cmd!r}.")
    return value


def now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def union_fieldnames(rows: List[Dict[str, Any]]) -> List[str]:
    fieldnames: List[str] = []
    seen = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)
    return fieldnames


def save_rows(rows: List[Dict[str, Any]], path: Path) -> None:
    if not rows:
        return
    ensure_dir(path.parent)
    fieldnames = union_fieldnames(rows)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def linear_points(start: float, stop: float, step: float) -> List[float]:
    if step <= 0:
        raise ValueError("Step size must be positive.")
    if stop < start:
        raise ValueError("Stop value must be larger than or equal to start value.")
    points: List[float] = []
    value = start
    while value <= stop + 1e-12:
        points.append(round(value, 12))
        value += step
    return points


def logspace_points(f_start: float, f_stop: float, points_per_decade: int, minimum_points: int = 2) -> List[float]:
    if f_start <= 0 or f_stop <= 0:
        raise ValueError("Frequency limits must be positive.")
    if f_stop <= f_start:
        raise ValueError("Stop frequency must be larger than start frequency.")
    if points_per_decade <= 0:
        raise ValueError("Points per decade must be positive.")
    if minimum_points <= 0:
        raise ValueError("Minimum frequency points must be positive.")
    decades = math.log10(f_stop) - math.log10(f_start)
    n_points = int(math.ceil(decades * points_per_decade)) + 1
    n_points = max(2, int(minimum_points), n_points)
    return [
        10 ** (math.log10(f_start) + k * decades / (n_points - 1))
        for k in range(n_points)
    ]


def wrap_phase_deg(phi: float) -> float:
    return ((phi + 180.0) % 360.0) - 180.0


def normalize_signed_phasor(magnitude: float, phase_deg: float) -> Tuple[float, float]:
    if magnitude < 0:
        magnitude = -magnitude
        phase_deg = wrap_phase_deg(phase_deg + 180.0)
    return magnitude, wrap_phase_deg(phase_deg)


def invert_phasor(magnitude: float, phase_deg: float) -> Tuple[float, float]:
    return abs(magnitude), wrap_phase_deg(phase_deg + 180.0)


def impedance_from_mag_phase(
    vac_mag: float,
    vac_phase_deg: float,
    iac_mag: float,
    iac_phase_deg: float,
    min_iac: float,
) -> Tuple[float, float, float, float]:
    if iac_mag <= min_iac:
        raise ValueError(
            f"Iac magnitude {iac_mag:.6e} A is too small, minimum is {min_iac:.6e} A."
        )
    z_mag = vac_mag / iac_mag
    z_phase_deg = wrap_phase_deg(vac_phase_deg - iac_phase_deg)
    z_phase_rad = math.radians(z_phase_deg)
    z_real = z_mag * math.cos(z_phase_rad)
    z_imag = z_mag * math.sin(z_phase_rad)
    return z_mag, z_phase_deg, z_real, z_imag


def capacitance_from_impedance(
    z_real: float,
    z_imag: float,
    frequency_hz: float,
) -> Tuple[float, float, float]:
    omega = 2.0 * math.pi * frequency_hz
    z_complex = complex(z_real, z_imag)
    if abs(z_complex) <= 0 or omega <= 0:
        return float("nan"), float("nan"), float("nan")
    y_complex = 1.0 / z_complex
    c_uncorrected = y_complex.imag / omega
    return c_uncorrected, y_complex.real, y_complex.imag


def circular_mean_deg(values: List[float]) -> float:
    if not values:
        return float("nan")
    sin_mean = sum(math.sin(math.radians(value)) for value in values) / len(values)
    cos_mean = sum(math.cos(math.radians(value)) for value in values) / len(values)
    return wrap_phase_deg(math.degrees(math.atan2(sin_mean, cos_mean)))


def aggregate_impedance_samples(
    samples: List[Dict[str, float]],
    max_spread_percent: float,
) -> Optional[Dict[str, Any]]:
    """Reject unstable samples and median-average the accepted complex impedance."""
    if not samples:
        return None

    center_real = statistics.median(sample["Z_real_ohm"] for sample in samples)
    center_imag = statistics.median(sample["Z_imag_ohm"] for sample in samples)
    center_magnitude = max(math.hypot(center_real, center_imag), 1e-30)
    spread_percent = [
        math.hypot(
            sample["Z_real_ohm"] - center_real,
            sample["Z_imag_ohm"] - center_imag,
        )
        / center_magnitude
        * 100.0
        for sample in samples
    ]
    accepted = [
        sample
        for sample, spread in zip(samples, spread_percent)
        if len(samples) == 1 or spread <= max_spread_percent
    ]
    required_accepted = max(1, len(samples) // 2 + 1)
    stable = len(accepted) >= required_accepted
    if not accepted:
        closest_index = min(range(len(samples)), key=lambda index: spread_percent[index])
        accepted = [samples[closest_index]]

    z_real = statistics.median(sample["Z_real_ohm"] for sample in accepted)
    z_imag = statistics.median(sample["Z_imag_ohm"] for sample in accepted)
    z_magnitude = math.hypot(z_real, z_imag)
    z_phase = wrap_phase_deg(math.degrees(math.atan2(z_imag, z_real)))

    result: Dict[str, Any] = {
        "Z_real_ohm": z_real,
        "Z_imag_ohm": z_imag,
        "Z_magnitude_ohm": z_magnitude,
        "Z_mag_ohm": z_magnitude,
        "Z_phase_deg": z_phase,
        "ac_sample_count_collected": len(samples),
        "ac_sample_count_accepted": len(accepted),
        "ac_sample_count_rejected": len(samples) - len(accepted),
        "ac_sample_required_accepted": required_accepted,
        "ac_impedance_spread_max_percent": max(spread_percent),
        "ac_impedance_spread_median_percent": statistics.median(spread_percent),
        "ac_impedance_samples_stable": stable,
        "ac_aggregation": "median accepted complex impedance",
    }
    for key in (
        "Vac_mag_raw_V",
        "Vac_mag_corrected_V",
        "Iac_mag_raw_A",
        "Iac_mag_corrected_A",
    ):
        result[key] = statistics.median(sample[key] for sample in accepted)
    for key in (
        "Vac_phase_raw_deg",
        "Vac_phase_corrected_deg",
        "Iac_phase_raw_deg",
        "Iac_phase_corrected_deg",
    ):
        result[key] = circular_mean_deg([sample[key] for sample in accepted])
    return result


def capacitance_scale_factor(unit: str) -> Tuple[float, str]:
    unit_l = unit.lower().replace("Âµ", "u")
    if unit_l == "f":
        return 1.0, "F"
    if unit_l == "mf":
        return 1e3, "mF"
    if unit_l == "uf":
        return 1e6, "uF"
    if unit_l == "nf":
        return 1e9, "nF"
    raise ValueError("Capacitance unit must be F, mF, uF, or nF.")


def format_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    minutes = int(seconds // 60)
    sec = int(round(seconds % 60))
    hours = minutes // 60
    minutes = minutes % 60
    if hours > 0:
        return f"{hours:d} h {minutes:02d} min"
    return f"{minutes:d} min {sec:02d} s"


def safe_log_value(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except Exception:
        return None
    if not math.isfinite(f):
        return None
    return f


# ============================================================================
# Settings
# ============================================================================


@dataclass(frozen=True)
class SpeedLevel:
    label: str
    points_per_decade: int
    minimum_frequency_points: int
    ac_samples_per_frequency: int
    ac_max_impedance_spread_percent: float
    ac_sample_interval_s: float
    settling_multiplier: float


SPEED_LEVELS: Dict[str, SpeedLevel] = {
    "Custom": SpeedLevel("Custom", 8, 8, 3, 8.0, 0.10, 1.0),
    "Fast": SpeedLevel("Fast", 4, 6, 1, 15.0, 0.00, 1.0),
    "Medium": SpeedLevel("Medium", 8, 10, 3, 8.0, 0.10, 1.0),
    "Slow": SpeedLevel("Slow", 16, 16, 5, 4.0, 0.20, 1.0),
}

AUTO_VDC_STEP_BY_SPEED: Dict[str, float] = {
    "Custom": 0.025,
    "Fast": 0.05,
    "Medium": 0.025,
    "Slow": 0.01,
}

AUTO_SMU_SWEEP_CACHE: Dict[Tuple[str, float, float, float], List[float]] = {}
AUTO_SMU_SWEEP_ROW_CACHE: Dict[Tuple[str, float, float, float], List[Dict[str, Any]]] = {}
AUTO_SMU_SWEEP_MPP_READY_CACHE: set[Tuple[str, float, float, float]] = set()


@dataclass
class Settings:
    # VISA addresses
    dmm_addr: str = "GPIB0::10::INSTR"
    lockin_i_addr: str = "GPIB0::15::INSTR"
    lockin_v_addr: str = "GPIB0::12::INSTR"
    fg_addr: str = "GPIB0::14::INSTR"
    led_fg_addr: str = "GPIB0::11::INSTR"
    smu_addr: str = "GPIB0::26::INSTR"

    # Output
    output_dir: Path = Path("measurement_output")
    simulation_mode: bool = False

    # SMU and DC sweep
    auto_smu_range: bool = True
    auto_smu_step_by_speed: bool = True
    smu_start_v: float = 11.0
    smu_stop_v: float = 15.0
    smu_step_v: float = 0.05
    cv_smu_step_v: float = 0.01
    pre_scan_step_v: float = 0.10
    max_smu_v: float = 15.0
    target_vpv_v: float = 1.0
    vdc_positive_threshold_v: float = 1e-4
    max_vdc_pv_v: float = 0.80
    stop_if_vdc_exceeds_max: bool = False
    stop_if_idc_negative: bool = True
    negative_idc_limit_a: float = -1e-6
    stop_if_idc_abs_exceeds_max: bool = True
    max_idc_abs_a: float = 10.0
    smu_current_limit_a: float = 0.5
    idc_adc1_to_ampere: float = 1.0
    idc_measurement_sign: float = 1.0
    dc_read_repeats: int = 3
    dc_variation_warning_percent: float = 2.0
    dc_vdc_variation_warning_floor_v: float = 0.001
    dc_idc_variation_warning_floor_a: float = 0.02

    # Manual operating point for impedance sweep
    operating_point_mode: str = "MPP_SEARCH"  # MPP_SEARCH or MANUAL_SMU_VOLTAGE
    manual_smu_voltage_v: float = 12.5
    min_mpp_vdc_pv_v: float = 0.0
    min_mpp_idc_pv_a: float = 0.0

    # Function generator and impedance sweep
    vac_vpp: float = 0.010
    fg_offset_v: float = 0.0
    fg_waveform: str = "SIN"
    freq_start_hz: float = 5.0
    freq_stop_hz: float = 10000.0
    custom_frequency_sweep_vdc_pv_step_size_v: float = 0.025
    custom_frequency_sweep_frequency_points_per_decade: int = 8
    custom_frequency_sweep_minimum_frequency_points: int = 8
    custom_frequency_sweep_settling_after_smu_s: float = 1.0
    custom_frequency_sweep_settling_after_freq_s: float = 4.0
    custom_frequency_sweep_lockin_time_constant_wait_s: float = 0.0
    custom_frequency_sweep_ac_samples_per_frequency: int = 3
    custom_frequency_sweep_ac_max_impedance_spread_percent: float = 8.0
    custom_frequency_sweep_ac_sample_interval_s: float = 0.10
    custom_cv_vdc_pv_step_size_v: float = 0.025
    custom_cv_frequency_points_per_decade: int = 8
    custom_cv_minimum_frequency_points: int = 8
    custom_cv_settling_after_smu_s: float = 1.0
    custom_cv_settling_after_freq_s: float = 4.0
    custom_cv_lockin_time_constant_wait_s: float = 0.0
    custom_cv_ac_samples_per_frequency: int = 3
    custom_cv_ac_max_impedance_spread_percent: float = 8.0
    custom_cv_ac_sample_interval_s: float = 0.10
    custom_vdc_pv_step_size_v: float = 0.025
    custom_frequency_points_per_decade: int = 8
    custom_minimum_frequency_points: int = 8
    settling_after_smu_s: float = 1.0
    settling_after_freq_s: float = 4.0
    lockin_time_constant_wait_s: float = 0.0
    ac_samples_per_frequency: int = 3
    ac_max_impedance_spread_percent: float = 8.0
    ac_sample_interval_s: float = 0.10
    min_iac_mag_a: float = 1e-12

    # LED modulation function generator
    led_duty_cycle_percent: float = 50.0

    # Outlier handling
    remeasure_z_real_outliers: bool = True
    max_abs_z_real_ohm: float = 100.0
    z_real_outlier_min_vdc_pv_v: float = 0.05
    max_outlier_retries: int = 8
    outlier_retry_wait_s: float = 1.0
    abort_if_outlier_retries_exhausted: bool = False

    # Phasor sign handling
    iac_measurement_sign: float = -1.0
    invert_current_phasor: bool = False
    invert_voltage_phasor: bool = False

    # Lock-in configuration
    configure_lockins: bool = True
    lockin_sensitivity_cmd: str = "SEN 21"

    # A-B monitor
    ab_sample_interval_s: float = 0.2
    ab_plot_window_points: int = 300

    # Plotting
    capacitance_unit: str = "uF"
    nyquist_y_axis_sign: float = 1.0

    # Output shutdown
    turn_off_smu_at_end: bool = False
    turn_off_fg_at_end: bool = False

    # Instrument commands
    iac_mag_cmd: str = "MAG."
    iac_phase_cmd: str = "PHA."
    idc_adc1_cmd: str = "ADC. 1"
    vac_mag_cmd: str = "MAG."
    vac_phase_cmd: str = "PHA."


@dataclass
class PreScanSummary:
    rows: List[Dict[str, Any]]
    first_positive_smu_v: Optional[float]
    stop_smu_v: Optional[float]
    estimated_cv_voltage_points: int
    elapsed_s: float


@dataclass
class RunResult:
    datasets: Dict[str, List[Dict[str, Any]]]
    output_files: List[Path]
    summary: Dict[str, Any]


# ============================================================================
# Optional simulation instruments
# ============================================================================


class FakeInstrument:
    def __init__(self, name: str, state: Dict[str, float]):
        self.name = name
        self.state = state
        self.timeout = 5000
        self.write_termination = "\n"
        self.read_termination = "\n"

    def write(self, cmd: str) -> None:
        cmd_l = cmd.strip().lower()
        if "levelv" in cmd_l:
            value = FLOAT_RE.findall(cmd)
            if value:
                self.state["smu_v"] = float(value[-1])
        elif cmd_l.startswith("freq"):
            value = FLOAT_RE.findall(cmd)
            if value:
                self.state["freq_hz"] = float(value[-1])
        elif cmd_l.startswith("volt "):
            value = FLOAT_RE.findall(cmd)
            if value:
                self.state["vac_vpp"] = float(value[-1])

    def query(self, cmd: str) -> str:
        smu = self.state.get("smu_v", 11.0)
        f_hz = max(1e-9, self.state.get("freq_hz", 100.0))
        vdc = max(-0.05, 0.19 * (smu - 11.0))
        idc = max(-0.02, 0.08 * math.exp(-2.0 * max(vdc, 0.0)) - 0.03 * max(vdc - 0.60, 0.0) * 10)
        if self.name == "dmm":
            return f"{vdc:.8e}"
        cmd_u = cmd.strip().upper()
        if cmd_u.startswith("ADC"):
            return f"{idc:.8e}"
        if cmd_u.startswith("MAG"):
            if self.name == "lockin_i":
                c = 20e-6
                r = 1.5
                vac_rms = self.state.get("vac_vpp", 0.01) / (2.0 * math.sqrt(2.0))
                omega = 2 * math.pi * f_hz
                y = complex(1.0 / r, omega * c)
                iac = abs(vac_rms * y)
                return f"{iac:.8e}"
            vac_rms = self.state.get("vac_vpp", 0.01) / (2.0 * math.sqrt(2.0))
            return f"{vac_rms:.8e}"
        if cmd_u.startswith("PHA"):
            if self.name == "lockin_i":
                phase = 10.0 + 60.0 / (1.0 + f_hz / 300.0)
                return f"{phase:.8e}"
            return "0.00000000e+00"
        if cmd_u.startswith("X"):
            if self.name == "lockin_v":
                return f"{-self.state.get('vac_vpp', 0.01) / (2.0 * math.sqrt(2.0)):.8e}"
            return f"{self.state.get('vac_vpp', 0.01) / (2.0 * math.sqrt(2.0)):.8e}"
        if cmd_u.startswith("SEN"):
            return "1.00000000e-02"
        return "0.00000000e+00"

    def clear(self) -> None:
        pass

    def close(self) -> None:
        pass


class FakeResourceManager:
    def __init__(self):
        self.state = {"smu_v": 11.0, "freq_hz": 100.0, "vac_vpp": 0.01}

    def open_resource(self, address: str) -> FakeInstrument:
        if "10" in address:
            return FakeInstrument("dmm", self.state)
        if "12" in address:
            return FakeInstrument("lockin_v", self.state)
        if "15" in address:
            return FakeInstrument("lockin_i", self.state)
        if "14" in address:
            return FakeInstrument("fg", self.state)
        if "11" in address:
            return FakeInstrument("led_fg", self.state)
        return FakeInstrument("smu", self.state)

    def list_resources(self) -> Tuple[str, ...]:
        return (
            "GPIB0::10::INSTR",
            "GPIB0::12::INSTR",
            "GPIB0::14::INSTR",
            "GPIB0::11::INSTR",
            "GPIB0::15::INSTR",
            "GPIB0::26::INSTR",
        )

    def close(self) -> None:
        pass


# ============================================================================
# VISA controller
# ============================================================================


class VisaController:
    def __init__(self, settings: Settings, log: Callable[[str], None]):
        self.settings = settings
        self.log = log
        self.rm: Any = None
        self.dmm: Any = None
        self.lockin_i: Any = None
        self.lockin_v: Any = None
        self.fg: Any = None
        self.led_fg: Any = None
        self.smu: Any = None
        self.last_dc_quality: Dict[str, Any] = {}

    def open(self, need_dmm=True, need_lockin_i=True, need_lockin_v=True, need_fg=True, need_smu=True, need_led_fg=True) -> None:
        if self.settings.simulation_mode:
            self.rm = FakeResourceManager()
            self.log("Simulation mode is ON. No hardware will be controlled.")
        else:
            if pyvisa is None:
                raise RuntimeError("pyvisa is not installed. Install it or use Simulation mode.")
            try:
                self.rm = pyvisa.ResourceManager()
            except Exception as exc:
                self.log("Default VISA backend failed; trying pyvisa-py backend '@py'.")
                try:
                    self.rm = pyvisa.ResourceManager("@py")
                except Exception:
                    raise RuntimeError(
                        "Could not locate a VISA implementation. "
                        "Install either a system VISA library or pyvisa-py. "
                        "If you already installed pyvisa-py, check that it is available in the same Python environment."
                    ) from exc
            try:
                resources = self.rm.list_resources()
                self.log(f"Available VISA resources: {resources}")
            except Exception:
                pass

        if need_dmm:
            self.dmm = self.rm.open_resource(self.settings.dmm_addr)
        if need_lockin_i:
            self.lockin_i = self.rm.open_resource(self.settings.lockin_i_addr)
        if need_lockin_v:
            self.lockin_v = self.rm.open_resource(self.settings.lockin_v_addr)
        if need_fg:
            self.fg = self.rm.open_resource(self.settings.fg_addr)
        if need_led_fg:
            self.led_fg = self.rm.open_resource(self.settings.led_fg_addr)
        if need_smu:
            self.smu = self.rm.open_resource(self.settings.smu_addr)

        for inst in [self.dmm, self.lockin_i, self.lockin_v, self.fg, self.led_fg, self.smu]:
            if inst is None:
                continue
            inst.timeout = 10000
            try:
                inst.write_termination = "\n"
                inst.read_termination = "\n"
            except Exception:
                pass
            try:
                inst.clear()
            except Exception:
                pass

    def close(self) -> None:
        for inst in [self.dmm, self.lockin_i, self.lockin_v, self.fg, self.led_fg, self.smu]:
            if inst is not None:
                try:
                    inst.close()
                except Exception:
                    pass
        if self.rm is not None:
            try:
                self.rm.close()
            except Exception:
                pass

    def safe_write(self, inst: Any, cmd: str, label: str) -> bool:
        try:
            inst.write(cmd)
            return True
        except Exception as exc:
            self.log(f"WARNING: {label} rejected command {cmd!r}: {exc}")
            return False

    def strict_write(self, inst: Any, cmd: str, label: str) -> None:
        try:
            inst.write(cmd)
        except Exception as exc:
            raise RuntimeError(f"{label} rejected critical command {cmd!r}: {exc}") from exc

    def query_float(self, inst: Any, cmd: str, label: str, retries: int = 3, delay: float = 0.2) -> float:
        last_error: Optional[BaseException] = None
        for attempt in range(1, retries + 1):
            try:
                raw = inst.query(cmd)
                return parse_float_reply(raw, label, cmd)
            except Exception as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(delay)
        raise ValueError(
            f"Failed to query {label} with command {cmd!r} after {retries} tries. Last error: {last_error}"
        )

    def configure_dmm(self) -> None:
        if self.dmm is None:
            return
        self.log("Configuring DMM...")
        self.safe_write(self.dmm, "*RST", "DMM")
        self.safe_write(self.dmm, "CONF:VOLT:DC", "DMM")

    def configure_smu(self, start_voltage: float) -> None:
        if self.smu is None:
            return
        if abs(start_voltage) > self.settings.max_smu_v:
            raise StopMeasurement(
                f"SMU start voltage {start_voltage:.6g} V exceeds max {self.settings.max_smu_v:.6g} V."
            )
        self.log("Configuring SMU...")
        self.strict_write(self.smu, "reset()", "SMU")
        self.strict_write(self.smu, "smua.source.func = smua.OUTPUT_DCVOLTS", "SMU")
        self.strict_write(self.smu, f"smua.source.limiti = {self.settings.smu_current_limit_a}", "SMU")
        self.strict_write(self.smu, f"smua.source.levelv = {start_voltage}", "SMU")
        self.strict_write(self.smu, "smua.source.output = smua.OUTPUT_ON", "SMU")
        time.sleep(self.settings.settling_after_smu_s)

    def configure_fg(self, initial_freq_hz: float) -> None:
        if self.fg is None:
            return
        self.log("Configuring function generator...")
        self.strict_write(self.fg, "*RST", "Function generator")
        self.strict_write(self.fg, f"FUNC {self.settings.fg_waveform}", "Function generator")
        self.strict_write(self.fg, f"VOLT {self.settings.vac_vpp}", "Function generator")
        self.strict_write(self.fg, f"VOLT:OFFS {self.settings.fg_offset_v}", "Function generator")
        self.strict_write(self.fg, f"FREQ {initial_freq_hz}", "Function generator")
        self.strict_write(self.fg, "OUTP ON", "Function generator")

    def configure_led_fg(self) -> None:
        if self.led_fg is None:
            return
        duty = min(99.0, max(1.0, float(self.settings.led_duty_cycle_percent)))
        self.log(f"Configuring LED function generator, GPIB 11: pulse, 1 MHz, 5 Vpp, 2.5 V offset, duty={duty:.3g}%.")
        commands = [
            "SOUR1:FUNC:SHAP PULS",
            "SOUR1:FREQ 1.0E6",
            "SOUR1:VOLT:LEV:IMM:AMPL 5.0",
            "SOUR1:VOLT:LEV:IMM:OFFS 2.5",
            f"SOUR1:PULS:DCYC {duty}",
            "OUTP1:STAT ON",
        ]
        old_timeout = getattr(self.led_fg, "timeout", None)
        try:
            self.led_fg.timeout = min(int(old_timeout or 10000), 2500)
        except Exception:
            old_timeout = None
        try:
            for cmd in commands:
                self.strict_write(self.led_fg, cmd, "LED function generator")
        finally:
            if old_timeout is not None:
                try:
                    self.led_fg.timeout = old_timeout
                except Exception:
                    pass

    def set_led_duty_cycle(self, duty_cycle_percent: float) -> None:
        if self.led_fg is None:
            return
        duty = min(99.0, max(1.0, float(duty_cycle_percent)))
        self.strict_write(self.led_fg, f"SOUR1:PULS:DCYC {duty}", "LED function generator")
        self.strict_write(self.led_fg, "OUTP1:STAT ON", "LED function generator")

    def configure_lockins_for_impedance(self) -> None:
        if not self.settings.configure_lockins:
            return

        def configure_channel(inst: Any, label: str, description: str, cmds: List[str]) -> None:
            if inst is None:
                return
            self.log(description)
            old_timeout = getattr(inst, "timeout", None)
            try:
                inst.timeout = min(int(old_timeout or 10000), 2500)
            except Exception:
                old_timeout = None
            failures = 0
            try:
                for cmd in cmds:
                    if not self.safe_write(inst, cmd, label):
                        failures += 1
                    if failures >= 2:
                        raise RuntimeError(
                            f"{label} is not responding during configuration. "
                            "Check if all devices are turned on, booted correctly, connected to GPIB, "
                            "and the solar cell setup is connected."
                        )
            finally:
                if old_timeout is not None:
                    try:
                        inst.timeout = old_timeout
                    except Exception:
                        pass

        if self.lockin_v is not None:
            configure_channel(
                self.lockin_v,
                "Lock-in voltage",
                "Configuring lock-in voltage channel, GPIB 12, A-B differential...",
                ["IMODE 0", "VMODE 3", "IE 1", self.settings.lockin_sensitivity_cmd],
            )
        if self.lockin_i is not None:
            configure_channel(
                self.lockin_i,
                "Lock-in current",
                "Configuring lock-in current channel, GPIB 15, A input...",
                ["IMODE 0", "VMODE 1", "IE 1", self.settings.lockin_sensitivity_cmd],
            )

    def set_smu_voltage(self, smu_voltage: float) -> None:
        if abs(smu_voltage) > self.settings.max_smu_v:
            raise StopMeasurement(
                f"Requested SMU voltage {smu_voltage:.6g} V exceeds max {self.settings.max_smu_v:.6g} V."
            )
        self.strict_write(self.smu, f"smua.source.levelv = {smu_voltage}", "SMU")

    def read_dc(self, repeats: Optional[int] = None) -> Tuple[float, float, float]:
        sample_count = max(1, int(self.settings.dc_read_repeats if repeats is None else repeats))
        vdc_samples: List[float] = []
        idc_samples: List[float] = []
        idc_raw_samples: List[float] = []
        for _ in range(sample_count):
            vdc_pv = self.query_float(self.dmm, "READ?", "DMM")
            idc_adc1_raw = self.query_float(self.lockin_i, self.settings.idc_adc1_cmd, "Lock-in current ADC1")
            idc_pv = idc_adc1_raw * self.settings.idc_adc1_to_ampere * self.settings.idc_measurement_sign
            self.check_dc_safety(vdc_pv, idc_pv)
            vdc_samples.append(vdc_pv)
            idc_samples.append(idc_pv)
            idc_raw_samples.append(idc_adc1_raw)

        vdc_median = statistics.median(vdc_samples)
        idc_median = statistics.median(idc_samples)
        idc_raw_median = statistics.median(idc_raw_samples)
        minimum_idc_index = min(range(sample_count), key=lambda index: idc_samples[index])
        minimum_idc = idc_samples[minimum_idc_index]
        vdc_range = max(vdc_samples) - min(vdc_samples)
        idc_range = max(idc_samples) - min(idc_samples)
        warning_percent = self.settings.dc_variation_warning_percent
        vdc_allowed = max(
            self.settings.dc_vdc_variation_warning_floor_v,
            abs(vdc_median) * warning_percent / 100.0,
        )
        idc_allowed = max(
            self.settings.dc_idc_variation_warning_floor_a,
            abs(idc_median) * warning_percent / 100.0,
        )
        warning_reasons = []
        if sample_count > 1 and vdc_range > vdc_allowed:
            warning_reasons.append(f"Vdc range {vdc_range:.6e} V exceeds {vdc_allowed:.6e} V")
        if sample_count > 1 and idc_range > idc_allowed:
            warning_reasons.append(f"Idc range {idc_range:.6e} A exceeds {idc_allowed:.6e} A")
        self.last_dc_quality = {
            "dc_sample_count": sample_count,
            "dc_aggregation": "median",
            "Vdc_pv_sample_min_V": min(vdc_samples),
            "Vdc_pv_sample_max_V": max(vdc_samples),
            "Vdc_pv_sample_range_V": vdc_range,
            "Vdc_pv_sample_spread_percent": vdc_range / max(abs(vdc_median), self.settings.dc_vdc_variation_warning_floor_v) * 100.0,
            "Idc_pv_sample_min_A": min(idc_samples),
            "Idc_pv_sample_max_A": max(idc_samples),
            "Idc_pv_sample_range_A": idc_range,
            "Idc_pv_sample_spread_percent": idc_range / max(abs(idc_median), self.settings.dc_idc_variation_warning_floor_a) * 100.0,
            "Idc_adc1_sample_min_raw": min(idc_raw_samples),
            "Idc_adc1_sample_max_raw": max(idc_raw_samples),
            "negative_idc_detected": minimum_idc < self.settings.negative_idc_limit_a,
            "negative_idc_sample_Vdc_pv_V": vdc_samples[minimum_idc_index],
            "negative_idc_sample_Idc_pv_A": minimum_idc,
            "negative_idc_sample_Idc_adc1_raw": idc_raw_samples[minimum_idc_index],
            "dc_quality_warning": bool(warning_reasons),
            "dc_quality_warning_reason": "; ".join(warning_reasons),
        }
        if warning_reasons:
            self.log("WARNING: Unstable DC reading: " + "; ".join(warning_reasons))
        return vdc_median, idc_median, idc_raw_median

    def dc_quality_fields(self) -> Dict[str, Any]:
        return dict(self.last_dc_quality)

    def negative_dc_sample(self) -> Optional[Tuple[float, float, float]]:
        if not self.last_dc_quality.get("negative_idc_detected"):
            return None
        return (
            float(self.last_dc_quality["negative_idc_sample_Vdc_pv_V"]),
            float(self.last_dc_quality["negative_idc_sample_Idc_pv_A"]),
            float(self.last_dc_quality["negative_idc_sample_Idc_adc1_raw"]),
        )

    def set_smu_voltage_and_read_dc(self, smu_voltage: float, wait_s: Optional[float] = None) -> Tuple[float, float, float]:
        self.set_smu_voltage(smu_voltage)
        time.sleep(self.settings.settling_after_smu_s if wait_s is None else wait_s)
        return self.read_dc()

    def check_dc_safety(self, vdc_pv: float, idc_pv: float) -> None:
        if abs(idc_pv) > self.settings.max_idc_abs_a:
            msg = (
                f"abs(Idc_pv) exceeded limit: {abs(idc_pv):.6e} A > "
                f"{self.settings.max_idc_abs_a:.6e} A."
            )
            if self.settings.stop_if_idc_abs_exceeds_max:
                raise StopMeasurement(msg)
            self.log("WARNING: " + msg + " Continuing because this safety stop is disabled.")
        if self.settings.stop_if_vdc_exceeds_max and vdc_pv > self.settings.max_vdc_pv_v:
            raise StopMeasurement(
                f"Vdc_pv exceeded limit: {vdc_pv:.6e} V > {self.settings.max_vdc_pv_v:.6e} V."
            )

    def read_ac_phasors(self) -> Dict[str, float]:
        iac_mag_raw = self.query_float(self.lockin_i, self.settings.iac_mag_cmd, "Lock-in current magnitude")
        iac_phase_raw = self.query_float(self.lockin_i, self.settings.iac_phase_cmd, "Lock-in current phase")
        iac_mag, iac_phase = normalize_signed_phasor(iac_mag_raw * self.settings.iac_measurement_sign, iac_phase_raw)
        if self.settings.invert_current_phasor:
            iac_mag, iac_phase = invert_phasor(iac_mag, iac_phase)

        vac_mag_raw = self.query_float(self.lockin_v, self.settings.vac_mag_cmd, "Lock-in voltage magnitude")
        vac_phase_raw = self.query_float(self.lockin_v, self.settings.vac_phase_cmd, "Lock-in voltage phase")
        vac_mag, vac_phase = normalize_signed_phasor(vac_mag_raw, vac_phase_raw)
        if self.settings.invert_voltage_phasor:
            vac_mag, vac_phase = invert_phasor(vac_mag, vac_phase)

        return {
            "Iac_mag_raw_A": iac_mag_raw,
            "Iac_phase_raw_deg": iac_phase_raw,
            "Iac_mag_corrected_A": iac_mag,
            "Iac_phase_corrected_deg": iac_phase,
            "Vac_mag_raw_V": vac_mag_raw,
            "Vac_phase_raw_deg": vac_phase_raw,
            "Vac_mag_corrected_V": vac_mag,
            "Vac_phase_corrected_deg": vac_phase,
        }

    def shutdown_outputs(self) -> None:
        if self.fg is not None:
            self.safe_write(self.fg, "OUTP ON", "Function generator")
            self.log("Function generator output left ON after run.")
        if self.led_fg is not None:
            self.safe_write(self.led_fg, "OUTP1:STAT ON", "LED function generator")
            self.log("LED function generator output left ON after run.")
        if self.smu is not None:
            self.safe_write(self.smu, f"smua.source.levelv = {self.settings.smu_stop_v}", "SMU")
            self.safe_write(self.smu, "smua.source.output = smua.OUTPUT_ON", "SMU")
            self.log(f"SMU output left ON at stop voltage: {self.settings.smu_stop_v:.6g} V")


# ============================================================================
# Measurement engine
# ============================================================================


class MeasurementEngine:
    def __init__(
        self,
        settings: Settings,
        log: Callable[[str], None],
        stop_event: threading.Event,
        live_callback: Optional[Callable[[str, List[Dict[str, Any]]], None]] = None,
        live_control_getter: Optional[Callable[[], Dict[str, Any]]] = None,
    ):
        self.settings = settings
        self.log = log
        self.stop_event = stop_event
        self.live_callback = live_callback
        self.live_control_getter = live_control_getter
        self.last_pre_scan: Optional[PreScanSummary] = None

    def check_stop(self) -> None:
        if self.stop_event.is_set():
            raise UserStop("Measurement stopped by user.")

    def validate(self) -> None:
        if abs(self.settings.smu_start_v) > self.settings.max_smu_v:
            raise ValueError("SMU start voltage exceeds the maximum SMU voltage.")
        if abs(self.settings.smu_stop_v) > self.settings.max_smu_v:
            raise ValueError("SMU stop voltage exceeds the maximum SMU voltage.")
        if self.settings.smu_current_limit_a <= 0:
            raise ValueError("SMU current limit must be positive.")
        if self.settings.max_idc_abs_a <= 0:
            raise ValueError("Max |Idc| safety limit must be positive.")
        if self.settings.idc_adc1_to_ampere <= 0:
            raise ValueError("IDC ADC1 to ampere scaling must be positive.")
        if self.settings.idc_measurement_sign not in (-1.0, 1.0):
            raise ValueError("IDC measurement sign must be +1 or -1.")
        if self.settings.dc_read_repeats < 1:
            raise ValueError("DC read repeats must be at least one.")
        if self.settings.dc_variation_warning_percent < 0:
            raise ValueError("DC variation warning percentage cannot be negative.")
        if self.settings.dc_vdc_variation_warning_floor_v < 0 or self.settings.dc_idc_variation_warning_floor_a < 0:
            raise ValueError("DC variation warning floors cannot be negative.")
        if self.settings.iac_measurement_sign not in (-1.0, 1.0):
            raise ValueError("IAC measurement sign must be +1 or -1.")
        if self.settings.nyquist_y_axis_sign not in (-1.0, 1.0):
            raise ValueError("Nyquist Y-axis sign must be +1 or -1.")
        if self.settings.custom_vdc_pv_step_size_v <= 0:
            raise ValueError("Custom Vdc_pv step size must be positive.")
        if self.settings.custom_frequency_points_per_decade <= 0:
            raise ValueError("Custom frequency points per decade must be positive.")
        if self.settings.custom_minimum_frequency_points <= 0:
            raise ValueError("Custom minimum frequency points must be positive.")
        if self.settings.ac_samples_per_frequency < 1:
            raise ValueError("AC samples per frequency must be at least one.")
        if self.settings.ac_max_impedance_spread_percent <= 0:
            raise ValueError("Maximum AC impedance spread must be positive.")
        if self.settings.ac_sample_interval_s < 0:
            raise ValueError("AC sample interval cannot be negative.")
        if (
            self.settings.settling_after_smu_s < 0
            or self.settings.settling_after_freq_s < 0
            or self.settings.lockin_time_constant_wait_s < 0
        ):
            raise ValueError("Settling and lock-in timing values cannot be negative.")
        for label, step, points, minimum, settle_smu, settle_freq, lockin_wait in (
            (
                "Custom frequency sweep",
                self.settings.custom_frequency_sweep_vdc_pv_step_size_v,
                self.settings.custom_frequency_sweep_frequency_points_per_decade,
                self.settings.custom_frequency_sweep_minimum_frequency_points,
                self.settings.custom_frequency_sweep_settling_after_smu_s,
                self.settings.custom_frequency_sweep_settling_after_freq_s,
                self.settings.custom_frequency_sweep_lockin_time_constant_wait_s,
            ),
            (
                "Custom CV curve",
                self.settings.custom_cv_vdc_pv_step_size_v,
                self.settings.custom_cv_frequency_points_per_decade,
                self.settings.custom_cv_minimum_frequency_points,
                self.settings.custom_cv_settling_after_smu_s,
                self.settings.custom_cv_settling_after_freq_s,
                self.settings.custom_cv_lockin_time_constant_wait_s,
            ),
        ):
            if step <= 0:
                raise ValueError(f"{label} Vdc_pv step size must be positive.")
            if points <= 0:
                raise ValueError(f"{label} frequency points per decade must be positive.")
            if minimum <= 0:
                raise ValueError(f"{label} minimum frequency points must be positive.")
            if settle_smu < 0 or settle_freq < 0 or lockin_wait < 0:
                raise ValueError(f"{label} timing values cannot be negative.")
        for label, samples, max_spread, sample_interval in (
            (
                "Custom frequency sweep",
                self.settings.custom_frequency_sweep_ac_samples_per_frequency,
                self.settings.custom_frequency_sweep_ac_max_impedance_spread_percent,
                self.settings.custom_frequency_sweep_ac_sample_interval_s,
            ),
            (
                "Custom CV curve",
                self.settings.custom_cv_ac_samples_per_frequency,
                self.settings.custom_cv_ac_max_impedance_spread_percent,
                self.settings.custom_cv_ac_sample_interval_s,
            ),
        ):
            if samples < 1:
                raise ValueError(f"{label} AC samples per frequency must be at least one.")
            if max_spread <= 0:
                raise ValueError(f"{label} maximum AC impedance spread must be positive.")
            if sample_interval < 0:
                raise ValueError(f"{label} AC sample interval cannot be negative.")
        if self.settings.max_outlier_retries < 0:
            raise ValueError("Maximum outlier retries must be zero or positive.")
        if self.settings.z_real_outlier_min_vdc_pv_v < 0:
            raise ValueError("Z' outlier minimum Vdc_pv must be zero or positive.")
        capacitance_scale_factor(self.settings.capacitance_unit)

    def auto_smu_step_enabled(self) -> bool:
        return bool(self.settings.auto_smu_range and self.settings.auto_smu_step_by_speed)

    def target_vdc_step_for_speed(self, speed_name: str) -> float:
        if speed_name == "Custom":
            return float(self.settings.custom_vdc_pv_step_size_v)
        return AUTO_VDC_STEP_BY_SPEED.get(speed_name, AUTO_VDC_STEP_BY_SPEED["Medium"])

    def sync_custom_speed_profile_from_settings(self) -> None:
        AUTO_VDC_STEP_BY_SPEED["Custom"] = float(self.settings.custom_vdc_pv_step_size_v)
        SPEED_LEVELS["Custom"] = SpeedLevel(
            "Custom",
            points_per_decade=max(1, int(self.settings.custom_frequency_points_per_decade)),
            minimum_frequency_points=max(1, int(self.settings.custom_minimum_frequency_points)),
            ac_samples_per_frequency=max(1, int(self.settings.ac_samples_per_frequency)),
            ac_max_impedance_spread_percent=float(self.settings.ac_max_impedance_spread_percent),
            ac_sample_interval_s=max(0.0, float(self.settings.ac_sample_interval_s)),
            settling_multiplier=1.0,
        )

    def apply_ac_accuracy_for_speed(self, speed_name: str) -> None:
        level = SPEED_LEVELS.get(speed_name, SPEED_LEVELS["Medium"])
        self.settings.ac_samples_per_frequency = max(1, int(level.ac_samples_per_frequency))
        self.settings.ac_max_impedance_spread_percent = float(level.ac_max_impedance_spread_percent)
        self.settings.ac_sample_interval_s = max(0.0, float(level.ac_sample_interval_s))

    def auto_smu_cache_key(self, speed_name: str, start_v: float, stop_v: float) -> Tuple[str, float, float, float]:
        return (
            speed_name,
            round(float(start_v), 6),
            round(float(stop_v), 6),
            round(self.target_vdc_step_for_speed(speed_name), 6),
        )

    def cached_auto_smu_voltages(self, speed_name: str, start_v: float, stop_v: float) -> List[float]:
        key = self.auto_smu_cache_key(speed_name, start_v, stop_v)
        return list(AUTO_SMU_SWEEP_CACHE.get(key, []))

    def cached_auto_smu_mpp_rows(self, speed_name: str, start_v: float, stop_v: float) -> List[Dict[str, Any]]:
        key = self.auto_smu_cache_key(speed_name, start_v, stop_v)
        if key not in AUTO_SMU_SWEEP_MPP_READY_CACHE:
            return []
        return [
            dict(row)
            for row in AUTO_SMU_SWEEP_ROW_CACHE.get(key, [])
            if start_v - 1e-9 <= float(row.get("smu_voltage_V", float("nan"))) <= stop_v + 1e-9
        ]

    def remember_auto_smu_voltage(self, speed_name: str, start_v: float, stop_v: float, smu_v: float) -> None:
        key = self.auto_smu_cache_key(speed_name, start_v, stop_v)
        values = AUTO_SMU_SWEEP_CACHE.setdefault(key, [])
        rounded = round(float(smu_v), 6)
        if not values or abs(values[-1] - rounded) > 1e-9:
            values.append(rounded)

    def remember_auto_smu_row(self, speed_name: str, start_v: float, stop_v: float, row: Dict[str, Any]) -> None:
        self.remember_auto_smu_voltage(speed_name, start_v, stop_v, float(row["smu_voltage_V"]))
        key = self.auto_smu_cache_key(speed_name, start_v, stop_v)
        cached_rows = AUTO_SMU_SWEEP_ROW_CACHE.setdefault(key, [])
        rounded = round(float(row["smu_voltage_V"]), 6)
        stored_row = dict(row)
        stored_row["smu_voltage_V"] = rounded
        for index, cached in enumerate(cached_rows):
            if abs(float(cached.get("smu_voltage_V", float("nan"))) - rounded) <= 1e-9:
                cached_rows[index] = stored_row
                break
        else:
            cached_rows.append(stored_row)
        cached_rows.sort(key=lambda item: float(item["smu_voltage_V"]))

    def mark_auto_smu_mpp_cache_ready(
        self,
        speed_name: str,
        start_v: float,
        stop_v: float,
        rows: List[Dict[str, Any]],
        stop_at_target_vpv: bool,
    ) -> None:
        if stop_at_target_vpv or not rows:
            return
        last = rows[-1]
        reached_stop = float(last["smu_voltage_V"]) >= stop_v - 1e-9
        reached_negative_current = (
            self.settings.stop_if_idc_negative
            and float(last["Idc_pv_A"]) < self.settings.negative_idc_limit_a
        )
        if reached_stop or reached_negative_current:
            key = self.auto_smu_cache_key(speed_name, start_v, stop_v)
            AUTO_SMU_SWEEP_MPP_READY_CACHE.add(key)

    def find_next_vdc_step(
        self,
        session: VisaController,
        low_row: Dict[str, Any],
        stop_v: float,
        target_vdc: float,
        label: str,
    ) -> Dict[str, Any]:
        low_v = float(low_row["smu_voltage_V"])
        if low_v >= stop_v - 1e-12:
            return low_row

        span = max(stop_v - self.settings.smu_start_v, 0.005)
        bracket_step = max(0.005, min(max(self.settings.smu_step_v, self.settings.cv_smu_step_v), span / 8.0))
        previous = low_row
        high: Optional[Dict[str, Any]] = None
        smu_v = low_v

        while smu_v < stop_v - 1e-12:
            self.check_stop()
            smu_v = min(stop_v, smu_v + bracket_step)
            vdc, idc, idc_raw = session.set_smu_voltage_and_read_dc(smu_v, wait_s=0.15)
            row = {
                "smu_voltage_V": smu_v,
                "Vdc_pv_V": vdc,
                "Idc_adc1_raw": idc_raw,
                "Idc_pv_A": idc,
                "Pdc_pv_W": vdc * idc,
                **session.dc_quality_fields(),
            }
            if vdc >= target_vdc or smu_v >= stop_v - 1e-12 or (
                self.settings.stop_if_idc_negative and idc < self.settings.negative_idc_limit_a
            ):
                high = row
                break
            previous = row

        if high is None:
            return previous
        if high["smu_voltage_V"] >= stop_v - 1e-12 and high["Vdc_pv_V"] < target_vdc:
            return high
        if self.settings.stop_if_idc_negative and high["Idc_pv_A"] < self.settings.negative_idc_limit_a:
            return high

        low = previous
        best = high
        for _ in range(10):
            self.check_stop()
            low_smu = float(low["smu_voltage_V"])
            high_smu = float(high["smu_voltage_V"])
            if high_smu - low_smu <= 0.0025:
                break
            mid_smu = round((low_smu + high_smu) / 2.0, 6)
            vdc, idc, idc_raw = session.set_smu_voltage_and_read_dc(mid_smu, wait_s=0.15)
            mid = {
                "smu_voltage_V": mid_smu,
                "Vdc_pv_V": vdc,
                "Idc_adc1_raw": idc_raw,
                "Idc_pv_A": idc,
                "Pdc_pv_W": vdc * idc,
                **session.dc_quality_fields(),
            }
            if abs(vdc - target_vdc) < abs(best["Vdc_pv_V"] - target_vdc):
                best = mid
            if vdc >= target_vdc:
                high = mid
            else:
                low = mid

        self.log(
            f"Auto SMU step {label} | target Vdc={target_vdc:.6e} V | "
            f"SMU={best['smu_voltage_V']:.6g} V | Vdc={best['Vdc_pv_V']:.6e} V"
        )
        return best

    def adaptive_dc_sweep_rows(
        self,
        session: VisaController,
        speed_name: str,
        start_v: float,
        stop_v: float,
        label: str,
        stop_at_target_vpv: bool = True,
    ) -> List[Dict[str, Any]]:
        target_step = self.target_vdc_step_for_speed(speed_name)
        self.log(
            f"Automatic SMU step size is ON. Target spacing for {speed_name}: "
            f"{target_step:.6g} Vdc_pv."
        )
        rows: List[Dict[str, Any]] = []
        point_index = 1

        def finish() -> List[Dict[str, Any]]:
            self.mark_auto_smu_mpp_cache_ready(speed_name, start_v, stop_v, rows, stop_at_target_vpv)
            return rows

        cached_voltages = [
            smu_v for smu_v in self.cached_auto_smu_voltages(speed_name, start_v, stop_v)
            if start_v - 1e-9 <= smu_v <= stop_v + 1e-9
        ]
        if cached_voltages:
            self.log(
                f"Reusing {len(cached_voltages)} cached automatic SMU voltage points for "
                f"{speed_name} over {start_v:.6g} V to {stop_v:.6g} V."
            )
        else:
            cached_voltages = [start_v]

        current: Optional[Dict[str, Any]] = None
        for smu_v in cached_voltages:
            self.check_stop()
            vdc, idc, idc_raw = session.set_smu_voltage_and_read_dc(smu_v)
            current = {
                "smu_voltage_V": smu_v,
                "Vdc_pv_V": vdc,
                "Idc_adc1_raw": idc_raw,
                "Idc_pv_A": idc,
                "Pdc_pv_W": vdc * idc,
                **session.dc_quality_fields(),
            }
            rows.append(current)
            self.remember_auto_smu_row(speed_name, start_v, stop_v, current)
            self.log(
                f"{label} auto {point_index:>3} | SMU={smu_v:.6g} V | "
                f"Vdc={vdc:.6e} V | Idc={idc:.6e} A | P={vdc * idc:.6e} W"
            )
            point_index += 1
            if stop_at_target_vpv and current["Vdc_pv_V"] >= self.settings.target_vpv_v:
                self.log(f"{label} auto sweep stopped because target Vpv was reached.")
                return finish()
            if self.settings.stop_if_idc_negative and current["Idc_pv_A"] < self.settings.negative_idc_limit_a:
                self.log(f"{label} auto sweep stopped because Idc became negative.")
                return finish()

        if current is None:
            raise RuntimeError("Automatic SMU sweep could not establish a starting point.")

        while current["smu_voltage_V"] < stop_v - 1e-12:
            if stop_at_target_vpv and current["Vdc_pv_V"] >= self.settings.target_vpv_v:
                self.log(f"{label} auto sweep stopped because target Vpv was reached.")
                break
            if self.settings.stop_if_idc_negative and current["Idc_pv_A"] < self.settings.negative_idc_limit_a:
                self.log(f"{label} auto sweep stopped because Idc became negative.")
                break
            target_vdc = current["Vdc_pv_V"] + target_step
            next_row = self.find_next_vdc_step(session, current, stop_v, target_vdc, label)
            if next_row["smu_voltage_V"] <= current["smu_voltage_V"] + 1e-12:
                break
            current = next_row
            rows.append(current)
            self.remember_auto_smu_row(speed_name, start_v, stop_v, current)
            self.log(
                f"{label} auto {point_index:>3} | SMU={current['smu_voltage_V']:.6g} V | "
                f"Vdc={current['Vdc_pv_V']:.6e} V | Idc={current['Idc_pv_A']:.6e} A | "
                f"P={current['Pdc_pv_W']:.6e} W"
            )
            point_index += 1
            if point_index > 2000:
                raise StopMeasurement("Automatic SMU step sweep exceeded 2000 points.")
        return finish()

    def estimate_cv_duration(self, pre_summary: Optional[PreScanSummary] = None) -> Dict[str, str]:
        self.sync_custom_speed_profile_from_settings()
        if pre_summary is None:
            n_voltage = max(1, len(linear_points(self.settings.smu_start_v, self.settings.smu_stop_v, self.settings.cv_smu_step_v)))
            pre_s = 0.0
        else:
            n_voltage = max(1, pre_summary.estimated_cv_voltage_points)
            pre_s = pre_summary.elapsed_s
        estimates: Dict[str, str] = {}
        for name, level in SPEED_LEVELS.items():
            if math.isclose(self.settings.freq_start_hz, self.settings.freq_stop_hz, rel_tol=0.0, abs_tol=1e-12):
                n_freq = 1
            else:
                n_freq = len(logspace_points(
                    self.settings.freq_start_hz,
                    self.settings.freq_stop_hz,
                    level.points_per_decade,
                    level.minimum_frequency_points,
                ))
            settle_freq = self.settings.settling_after_freq_s
            per_freq_s = (
                settle_freq
                + self.settings.lockin_time_constant_wait_s
                + level.ac_samples_per_frequency * 0.15
                + max(0, level.ac_samples_per_frequency - 1) * level.ac_sample_interval_s
            )
            total_s = pre_s + n_voltage * self.settings.settling_after_smu_s
            total_s += n_voltage * n_freq * per_freq_s
            estimates[name] = format_duration(total_s)
        return estimates

    def run_smu_range_calibration(self) -> RunResult:
        self.validate()
        AUTO_SMU_SWEEP_CACHE.clear()
        AUTO_SMU_SWEEP_ROW_CACHE.clear()
        AUTO_SMU_SWEEP_MPP_READY_CACHE.clear()
        self.log("Automatic SMU step cache cleared for calibration.")
        output_dir = self.settings.output_dir
        ensure_dir(output_dir)
        timestamp = now_tag()
        rows: List[Dict[str, Any]] = []
        session = VisaController(self.settings, self.log)

        def measure_at(smu_v: float, phase: str, step_v: float) -> Dict[str, Any]:
            self.check_stop()
            vdc, idc, idc_raw = session.set_smu_voltage_and_read_dc(smu_v, wait_s=0.15)
            row = {
                "timestamp": iso_now(),
                "phase": phase,
                "step_v": step_v,
                "smu_voltage_V": smu_v,
                "Vdc_pv_V": vdc,
                "Idc_adc1_raw": idc_raw,
                "Idc_pv_A": idc,
                "Pdc_pv_W": vdc * idc,
                **session.dc_quality_fields(),
            }
            rows.append(row)
            self.log(f"Calibrate {phase} | SMU={smu_v:.6g} V | Vdc={vdc:.6e} V | Idc={idc:.6e} A")
            return row

        def scan_boundary(
            start_v: float,
            stop_v: float,
            step_v: float,
            predicate: Callable[[Dict[str, Any]], bool],
            phase: str,
        ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
            previous: Optional[Dict[str, Any]] = None
            found: Optional[Dict[str, Any]] = None
            for smu_v in linear_points(start_v, stop_v, step_v):
                row = measure_at(smu_v, phase, step_v)
                if predicate(row):
                    found = row
                    break
                previous = row
            return previous, found

        def refine_boundary(
            low_v: float,
            high_v: float,
            predicate: Callable[[Dict[str, Any]], bool],
            label: str,
        ) -> Dict[str, Any]:
            step_v = max((high_v - low_v) / 5.0, 0.005)
            best: Optional[Dict[str, Any]] = None
            low = low_v
            high = high_v
            while step_v > 0.005 + 1e-12:
                before, found = scan_boundary(low, high, step_v, predicate, label)
                if found is None:
                    low = before["smu_voltage_V"] if before else low
                    high = min(self.settings.smu_stop_v, high + step_v)
                else:
                    best = found
                    low = before["smu_voltage_V"] if before else low
                    high = found["smu_voltage_V"]
                step_v = max(step_v / 5.0, 0.005)
            before, found = scan_boundary(low, high, 0.005, predicate, f"{label}_final")
            if found is not None:
                best = found
            if best is None:
                raise StopMeasurement(f"Calibration could not find {label} boundary.")
            return best

        def verify_or_find_forward(
            row: Dict[str, Any],
            predicate: Callable[[Dict[str, Any]], bool],
            label: str,
            search_stop_v: float,
        ) -> Dict[str, Any]:
            check = measure_at(row["smu_voltage_V"], f"check_{label}", 0.005)
            if predicate(check):
                return check
            self.log(
                f"Calibration verification at {row['smu_voltage_V']:.6g} V did not pass for {label}. "
                "Scanning forward in 5 mV steps."
            )
            start_v = min(row["smu_voltage_V"] + 0.005, search_stop_v)
            for smu_v in linear_points(start_v, search_stop_v, 0.005):
                candidate = measure_at(smu_v, f"verify_{label}", 0.005)
                if predicate(candidate):
                    check_candidate = measure_at(smu_v, f"check_{label}", 0.005)
                    if predicate(check_candidate):
                        return check_candidate
            raise StopMeasurement(f"Calibration verification failed: could not verify {label}.")

        try:
            session.open(need_dmm=True, need_lockin_i=True, need_lockin_v=False, need_fg=True, need_smu=True)
            session.configure_dmm()
            session.configure_fg(self.settings.freq_start_hz)
            session.configure_led_fg()
            session.configure_smu(self.settings.smu_start_v)
            span = max(0.005, self.settings.smu_stop_v - self.settings.smu_start_v)
            coarse_step = max(0.5, span / 8.0)
            self.log("Starting automatic SMU range calibration for solar cell...")

            positive_predicate = lambda row: row["Vdc_pv_V"] >= self.settings.vdc_positive_threshold_v
            negative_predicate = lambda row: row["Idc_pv_A"] < self.settings.negative_idc_limit_a

            before_pos, first_pos = scan_boundary(
                self.settings.smu_start_v,
                self.settings.smu_stop_v,
                coarse_step,
                positive_predicate,
                "coarse_positive_vdc",
            )
            if first_pos is None:
                raise StopMeasurement("Calibration could not find a positive Vdc_pv point.")
            start_low = before_pos["smu_voltage_V"] if before_pos else self.settings.smu_start_v
            start_row = refine_boundary(start_low, first_pos["smu_voltage_V"], positive_predicate, "positive_vdc")

            before_neg, first_neg = scan_boundary(
                start_row["smu_voltage_V"],
                self.settings.smu_stop_v,
                coarse_step,
                negative_predicate,
                "coarse_negative_idc",
            )
            if first_neg is None:
                raise StopMeasurement("Calibration could not find where Idc_pv becomes negative.")
            stop_low = before_neg["smu_voltage_V"] if before_neg else start_row["smu_voltage_V"]
            stop_row = refine_boundary(stop_low, first_neg["smu_voltage_V"], negative_predicate, "negative_idc")

            check_start = verify_or_find_forward(
                start_row,
                positive_predicate,
                "positive_vdc",
                min(self.settings.smu_stop_v, start_row["smu_voltage_V"] + 0.10),
            )
            check_stop = verify_or_find_forward(
                stop_row,
                negative_predicate,
                "negative_idc",
                min(self.settings.smu_stop_v, max(first_neg["smu_voltage_V"], stop_row["smu_voltage_V"] + 0.10)),
            )

            calibrated_start = round(check_start["smu_voltage_V"], 6)
            calibrated_stop = round(check_stop["smu_voltage_V"], 6)
            self.settings.smu_start_v = calibrated_start
            self.settings.smu_stop_v = calibrated_stop
            csv_path = output_dir / f"smu_range_calibration_{timestamp}.csv"
            save_rows(rows, csv_path)
            self.log(
                f"Calibration finished | SMU start={calibrated_start:.6g} V | "
                f"SMU stop={calibrated_stop:.6g} V | calibration precision=0.005 V"
            )
            return RunResult(
                datasets={"smu_range_calibration": rows},
                output_files=[csv_path],
                summary={
                    "smu_start_v": calibrated_start,
                    "smu_stop_v": calibrated_stop,
                    "positive_vdc_row": check_start,
                    "negative_idc_row": check_stop,
                },
            )
        finally:
            try:
                session.shutdown_outputs()
            finally:
                session.close()

    def run_pre_scan(self) -> RunResult:
        self.validate()
        output_dir = self.settings.output_dir
        ensure_dir(output_dir)
        timestamp = now_tag()
        rows: List[Dict[str, Any]] = []
        first_positive_smu: Optional[float] = None
        stop_smu: Optional[float] = None
        start_time = time.time()

        session = VisaController(self.settings, self.log)
        try:
            session.open(need_dmm=True, need_lockin_i=True, need_lockin_v=False, need_fg=True, need_smu=True)
            session.configure_dmm()
            session.configure_fg(self.settings.freq_start_hz)
            session.configure_led_fg()
            session.configure_smu(self.settings.smu_start_v)

            points = linear_points(self.settings.smu_start_v, self.settings.smu_stop_v, self.settings.pre_scan_step_v)
            self.log("Starting fast pre-measure voltage scan...")
            for idx, smu_v in enumerate(points, start=1):
                self.check_stop()
                vdc, idc, idc_raw = session.set_smu_voltage_and_read_dc(smu_v, wait_s=0.15)
                power = vdc * idc
                row = {
                    "timestamp": iso_now(),
                    "point_index": idx,
                    "smu_voltage_V": smu_v,
                    "Vdc_pv_V": vdc,
                    "Idc_adc1_raw": idc_raw,
                    "Idc_pv_A": idc,
                    "Pdc_pv_W": power,
                    **session.dc_quality_fields(),
                }
                rows.append(row)
                self.log(f"Pre-scan {idx:>3}/{len(points)} | SMU={smu_v:.4f} V | Vdc={vdc:.5e} V | Idc={idc:.5e} A")

                if first_positive_smu is None and vdc >= self.settings.vdc_positive_threshold_v:
                    first_positive_smu = smu_v
                if self.settings.stop_if_idc_negative and idc < self.settings.negative_idc_limit_a:
                    stop_smu = smu_v
                    self.log("Pre-scan stopped because Idc became negative.")
                    break
                if self.settings.stop_if_vdc_exceeds_max and vdc > self.settings.max_vdc_pv_v:
                    stop_smu = smu_v
                    self.log("Pre-scan stopped because Vdc exceeded the configured max.")
                    break
                if vdc >= self.settings.target_vpv_v:
                    stop_smu = smu_v
                    self.log("Pre-scan stopped because target Vpv was reached.")
                    break

            if stop_smu is None and rows:
                stop_smu = rows[-1]["smu_voltage_V"]
            if first_positive_smu is None and rows:
                first_positive_smu = rows[0]["smu_voltage_V"]

            if first_positive_smu is not None and stop_smu is not None and stop_smu >= first_positive_smu:
                n_cv_points = len(linear_points(first_positive_smu, stop_smu, self.settings.cv_smu_step_v))
            else:
                n_cv_points = max(1, len(rows))

            elapsed = time.time() - start_time
            summary = PreScanSummary(rows, first_positive_smu, stop_smu, n_cv_points, elapsed)
            self.last_pre_scan = summary
            csv_path = output_dir / f"pre_scan_{timestamp}.csv"
            save_rows(rows, csv_path)
            estimates = self.estimate_cv_duration(summary)
            self.log("Pre-scan finished.")
            self.log(f"Estimated CV points: {n_cv_points}")
            for name, duration in estimates.items():
                self.log(f"Estimated {name} CV duration: {duration}")
            return RunResult(
                datasets={"pre_scan": rows},
                output_files=[csv_path],
                summary={
                    "pre_scan": summary,
                    "cv_duration_estimates": estimates,
                },
            )
        finally:
            try:
                session.shutdown_outputs()
            finally:
                session.close()

    def run_iv_pv(self, speed_name: str = "Medium") -> RunResult:
        self.validate()
        output_dir = self.settings.output_dir
        ensure_dir(output_dir)
        timestamp = now_tag()
        rows: List[Dict[str, Any]] = []
        session = VisaController(self.settings, self.log)
        try:
            session.open(need_dmm=True, need_lockin_i=True, need_lockin_v=False, need_fg=True, need_smu=True)
            session.configure_dmm()
            session.configure_fg(self.settings.freq_start_hz)
            session.configure_led_fg()
            session.configure_smu(self.settings.smu_start_v)

            self.log("Starting IV/PV voltage sweep...")
            if self.auto_smu_step_enabled():
                measured_rows = self.adaptive_dc_sweep_rows(
                    session,
                    speed_name,
                    self.settings.smu_start_v,
                    self.settings.smu_stop_v,
                    "IV",
                    stop_at_target_vpv=False,
                )
                for idx, measured in enumerate(measured_rows, start=1):
                    rows.append({
                        "timestamp": iso_now(),
                        "point_index": idx,
                        **measured,
                        "is_negative_current_endpoint": measured["Idc_pv_A"] < self.settings.negative_idc_limit_a,
                        "auto_smu_step_by_speed": True,
                        "target_vdc_step_V": self.target_vdc_step_for_speed(speed_name),
                    })
            else:
                smu_points = linear_points(self.settings.smu_start_v, self.settings.smu_stop_v, self.settings.smu_step_v)
                for idx, smu_v in enumerate(smu_points, start=1):
                    self.check_stop()
                    vdc, idc, idc_raw = session.set_smu_voltage_and_read_dc(smu_v)
                    power = vdc * idc
                    rows.append({
                        "timestamp": iso_now(),
                        "point_index": idx,
                        "smu_voltage_V": smu_v,
                        "Vdc_pv_V": vdc,
                        "Idc_adc1_raw": idc_raw,
                        "Idc_pv_A": idc,
                        "Pdc_pv_W": power,
                        **session.dc_quality_fields(),
                        "is_negative_current_endpoint": idc < self.settings.negative_idc_limit_a,
                        "auto_smu_step_by_speed": False,
                    })
                    self.log(f"IV {idx:>3}/{len(smu_points)} | SMU={smu_v:.4f} V | Vdc={vdc:.5e} V | Idc={idc:.5e} A | P={power:.5e} W")

                    if self.settings.stop_if_idc_negative and idc < self.settings.negative_idc_limit_a:
                        self.log("IV sweep stopped because Idc became negative.")
                        break

            if not rows:
                raise RuntimeError("No IV/PV points were recorded.")
            if self.settings.stop_if_idc_negative and not any(row["Idc_pv_A"] < self.settings.negative_idc_limit_a for row in rows):
                self.log(
                    "WARNING: IV/PV sweep reached the configured SMU stop voltage before a negative-current endpoint was measured."
                )
            candidates = [r for r in rows if r["Idc_pv_A"] >= self.settings.min_mpp_idc_pv_a and r["Vdc_pv_V"] >= self.settings.min_mpp_vdc_pv_v]
            mpp_row = max(candidates or rows, key=lambda r: r["Pdc_pv_W"])
            self.log("Maximum power point from IV sweep:")
            self.log(f"  Vmp={mpp_row['Vdc_pv_V']:.6e} V | Imp={mpp_row['Idc_pv_A']:.6e} A | Pmax={mpp_row['Pdc_pv_W']:.6e} W | SMU={mpp_row['smu_voltage_V']:.6g} V")
            self.log(f"After the run, SMU will be left ON at stop voltage: {self.settings.smu_stop_v:.6g} V")

            csv_path = output_dir / f"iv_pv_sweep_{timestamp}.csv"
            save_rows(rows, csv_path)
            return RunResult(
                datasets={"iv_pv_sweep": rows},
                output_files=[csv_path],
                summary={"mpp_row": mpp_row},
            )
        finally:
            try:
                session.shutdown_outputs()
            finally:
                session.close()

    def measure_impedance_point(
        self,
        session: VisaController,
        f_ac: float,
        point_index: int,
        total_points: int,
        base_row: Dict[str, Any],
        end_if_negative_idc: bool,
        rejected_rows: List[Dict[str, Any]],
        settling_s: float,
        remeasure_z_real_outliers: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        session.strict_write(session.fg, f"FREQ {f_ac}", "Function generator")
        time.sleep(settling_s + self.settings.lockin_time_constant_wait_s)
        total_attempts = self.settings.max_outlier_retries + 1
        last_outlier_row: Optional[Dict[str, Any]] = None
        last_failure_message = ""
        should_remeasure_z_real = self.settings.remeasure_z_real_outliers if remeasure_z_real_outliers is None else remeasure_z_real_outliers
        for attempt in range(1, total_attempts + 1):
            self.check_stop()
            vdc, idc, idc_raw = session.read_dc()
            negative_sample = session.negative_dc_sample()
            if end_if_negative_idc and negative_sample is not None:
                negative_vdc, negative_idc, negative_idc_raw = negative_sample
                raise NegativeCurrentEndpoint(
                    negative_vdc,
                    negative_idc,
                    negative_idc_raw,
                    f_ac,
                    session.dc_quality_fields(),
                )
            requested_samples = max(1, int(self.settings.ac_samples_per_frequency))
            impedance_samples: List[Dict[str, float]] = []
            sample_errors: List[str] = []
            for sample_index in range(requested_samples):
                self.check_stop()
                try:
                    ph = session.read_ac_phasors()
                    iac_mag = ph["Iac_mag_corrected_A"]
                    if iac_mag <= self.settings.min_iac_mag_a:
                        sample_errors.append(
                            f"Iac {iac_mag:.6e} A <= minimum {self.settings.min_iac_mag_a:.6e} A"
                        )
                    else:
                        z_mag, z_phase, z_real, z_imag = impedance_from_mag_phase(
                            ph["Vac_mag_corrected_V"],
                            ph["Vac_phase_corrected_deg"],
                            ph["Iac_mag_corrected_A"],
                            ph["Iac_phase_corrected_deg"],
                            self.settings.min_iac_mag_a,
                        )
                        impedance_samples.append({
                            **ph,
                            "Z_real_ohm": z_real,
                            "Z_imag_ohm": z_imag,
                            "Z_magnitude_ohm": z_mag,
                            "Z_mag_ohm": z_mag,
                            "Z_phase_deg": z_phase,
                        })
                except Exception as exc:
                    sample_errors.append(str(exc))
                if sample_index + 1 < requested_samples and self.settings.ac_sample_interval_s > 0:
                    time.sleep(self.settings.ac_sample_interval_s)

            if not impedance_samples:
                if len(sample_errors) >= requested_samples and any(
                    "failed to query" in error.lower() or "invalid session" in error.lower()
                    for error in sample_errors
                ):
                    raise RuntimeError(
                        f"All {requested_samples} AC samples failed at {f_ac:.6g} Hz. "
                        f"Last error: {sample_errors[-1]}"
                    )
                last_failure_message = (
                    f"Frequency {f_ac:.6g} Hz skipped, no valid AC samples "
                    f"({len(sample_errors)} rejected)."
                )
                self.log(last_failure_message)
                if attempt < total_attempts:
                    time.sleep(self.settings.outlier_retry_wait_s + self.settings.lockin_time_constant_wait_s)
                    continue
                break

            aggregate = aggregate_impedance_samples(
                impedance_samples,
                self.settings.ac_max_impedance_spread_percent,
            )
            if aggregate is None:
                return None
            required_samples = max(1, requested_samples // 2 + 1)
            aggregate["ac_sample_count_requested"] = requested_samples
            aggregate["ac_sample_required_accepted"] = required_samples
            aggregate["ac_sample_read_errors"] = len(sample_errors)
            aggregate["ac_sample_error_summary"] = "; ".join(sample_errors)
            aggregate["ac_max_impedance_spread_allowed_percent"] = self.settings.ac_max_impedance_spread_percent
            aggregate["ac_sample_interval_s"] = self.settings.ac_sample_interval_s
            aggregate["ac_impedance_samples_stable"] = (
                aggregate["ac_sample_count_accepted"] >= required_samples
            )
            quality_reasons: List[str] = []
            if aggregate["ac_sample_count_rejected"]:
                quality_reasons.append(
                    f"{aggregate['ac_sample_count_rejected']} impedance sample(s) rejected by spread"
                )
            if sample_errors:
                quality_reasons.append(f"{len(sample_errors)} AC sample read(s) invalid")
            aggregate["ac_quality_warning"] = bool(quality_reasons)
            aggregate["ac_quality_warning_reason"] = "; ".join(quality_reasons)

            z_mag = aggregate["Z_magnitude_ohm"]
            z_phase = aggregate["Z_phase_deg"]
            z_real = aggregate["Z_real_ohm"]
            z_imag = aggregate["Z_imag_ohm"]
            cap_f, y_real, y_imag = capacitance_from_impedance(z_real, z_imag, f_ac)
            z_real_outlier_check_allowed = vdc >= self.settings.z_real_outlier_min_vdc_pv_v
            is_z_real_outlier = (
                should_remeasure_z_real
                and z_real_outlier_check_allowed
                and abs(z_real) > self.settings.max_abs_z_real_ohm
            )
            is_ac_unstable = not aggregate["ac_impedance_samples_stable"]
            is_outlier = is_z_real_outlier or is_ac_unstable
            rejection_reasons: List[str] = []
            if is_z_real_outlier:
                rejection_reasons.append(
                    f"Z'={z_real:.6e} ohm exceeds +/-{self.settings.max_abs_z_real_ohm:g} ohm"
                )
            if is_ac_unstable:
                rejection_reasons.append(
                    f"only {aggregate['ac_sample_count_accepted']}/{requested_samples} AC samples "
                    f"were within {self.settings.ac_max_impedance_spread_percent:g}% of median Z"
                )

            row = {
                **base_row,
                "timestamp": iso_now(),
                "point_index": point_index,
                "measurement_attempt": attempt,
                "outlier_retries_before_acceptance": attempt - 1,
                "is_rejected_Z_real_outlier": is_z_real_outlier,
                "is_rejected_ac_instability": is_ac_unstable,
                "measurement_rejection_reason": "; ".join(rejection_reasons),
                "z_real_outlier_check_allowed": z_real_outlier_check_allowed,
                "Vdc_pv_V": vdc,
                "Idc_adc1_raw": idc_raw,
                "Idc_pv_A": idc,
                "Pdc_pv_W": vdc * idc,
                **session.dc_quality_fields(),
                "f_ac_Hz": f_ac,
                **aggregate,
                "Y_real_S": y_real,
                "Y_imag_S": y_imag,
                "C_uncorrected_F": cap_f,
            }

            if not is_outlier:
                scale, unit_label = capacitance_scale_factor(self.settings.capacitance_unit)
                self.log(
                    f"Freq {point_index:>3}/{total_points} | f={f_ac:>10.6g} Hz | "
                    f"Z'={z_real:>11.4e} ohm | Z''={z_imag:>11.4e} ohm | "
                    f"|Z|={z_mag:>11.4e} ohm | phase={z_phase:>8.3f} deg | "
                    f"C={cap_f * scale:.6g} {unit_label} | "
                    f"AC samples={aggregate['ac_sample_count_accepted']}/{requested_samples} | "
                    f"spread={aggregate['ac_impedance_spread_max_percent']:.3g}% | attempt={attempt}"
                )
                return row

            rejected_rows.append(row)
            last_outlier_row = row
            last_failure_message = "; ".join(rejection_reasons)
            self.log(
                f"OUTLIER | f={f_ac:.6g} Hz | attempt={attempt}/{total_attempts} | "
                + "; ".join(rejection_reasons)
            )
            if attempt < total_attempts:
                time.sleep(self.settings.outlier_retry_wait_s + self.settings.lockin_time_constant_wait_s)

        if last_outlier_row is not None or last_failure_message:
            msg = (
                f"No acceptable impedance result at f={f_ac:.6g} Hz after {total_attempts} attempts."
            )
            if self.settings.abort_if_outlier_retries_exhausted:
                raise StopMeasurement(msg)

            # Do not kill the whole GUI run because one frequency repeatedly looks
            # like a Z' outlier. At low PV bias, a high real impedance can be a real
            # physical result instead of a spike. Keep the best available point,
            # mark it clearly, and let the later CV filter/plotting handle it.
            if last_outlier_row is not None:
                accepted_row = dict(last_outlier_row)
                accepted_row["accepted_after_outlier_retries_exhausted"] = True
                accepted_row["measurement_warning"] = msg
                self.log("WARNING: " + msg + " Last point was kept and marked.")
                return accepted_row
            self.log("WARNING: " + msg + " " + last_failure_message + " Point skipped.")
        return None

    def filter_capacitance_rows(self, rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        candidates = []
        for idx, row in enumerate(rows, start=1):
            f = safe_log_value(row.get("f_ac_Hz"))
            c = safe_log_value(row.get("C_uncorrected_F"))
            if f is None or c is None or f <= 0:
                continue
            if c <= 0:
                continue
            candidates.append({"point_index": idx, "frequency_hz": f, "capacitance_f": c})
        candidates.sort(key=lambda p: p["frequency_hz"])
        if not candidates:
            return None

        kept = candidates[:]
        for _ in range(5):
            values = [p["capacitance_f"] for p in kept]
            baseline = statistics.median(values)
            deviations = [abs(v - baseline) for v in values]
            mad = statistics.median(deviations)
            if mad <= 0:
                tolerance = abs(baseline) * 0.20 if baseline else 1e-30
            else:
                tolerance = 3.5 * 1.4826 * mad
            new_kept = [p for p in kept if abs(p["capacitance_f"] - baseline) <= tolerance]
            if len(new_kept) < 2 or len(new_kept) == len(kept):
                break
            kept = new_kept

        values = [p["capacitance_f"] for p in kept]
        freqs = [p["frequency_hz"] for p in kept]
        return {
            "ok": True,
            "final_median_F": statistics.median(values),
            "final_mean_F": sum(values) / len(values),
            "final_std_F": statistics.stdev(values) if len(values) > 1 else 0.0,
            "used_count": len(values),
            "candidate_count": len(candidates),
            "frequency_min_Hz": min(freqs),
            "frequency_max_Hz": max(freqs),
        }

    def run_cv(self, speed_name: str) -> RunResult:
        self.apply_ac_accuracy_for_speed(speed_name)
        self.validate()
        level = SPEED_LEVELS[speed_name]
        output_dir = self.settings.output_dir
        ensure_dir(output_dir)
        timestamp = now_tag()
        if math.isclose(self.settings.freq_start_hz, self.settings.freq_stop_hz, rel_tol=0.0, abs_tol=1e-12):
            if self.settings.freq_start_hz <= 0:
                raise ValueError("Single CV frequency must be positive.")
            freqs = [self.settings.freq_start_hz]
        else:
            freqs = logspace_points(
                self.settings.freq_start_hz,
                self.settings.freq_stop_hz,
                level.points_per_decade,
                level.minimum_frequency_points,
            )
        settling_s = self.settings.settling_after_freq_s
        cv_repeats = 1
        all_rows: List[Dict[str, Any]] = []
        cv_rows: List[Dict[str, Any]] = []
        rejected_rows: List[Dict[str, Any]] = []
        negative_current_endpoint: Optional[NegativeCurrentEndpoint] = None

        session = VisaController(self.settings, self.log)
        try:
            session.open(need_dmm=True, need_lockin_i=True, need_lockin_v=True, need_fg=True, need_smu=True)
            session.configure_dmm()
            session.configure_fg(freqs[0])
            session.configure_led_fg()
            session.configure_smu(self.settings.smu_start_v)
            session.configure_lockins_for_impedance()

            if self.last_pre_scan and self.last_pre_scan.first_positive_smu_v is not None:
                first_smu = self.last_pre_scan.first_positive_smu_v
                stop_smu = self.last_pre_scan.stop_smu_v or self.settings.smu_stop_v
                self.log(f"Using pre-scan range for CV: {first_smu:.6g} V to {stop_smu:.6g} V.")
            else:
                self.log("No pre-scan found. Finding first positive Vdc point before CV sweep...")
                first_smu = self.settings.smu_start_v
                stop_smu = self.settings.smu_stop_v
                for smu_v in linear_points(self.settings.smu_start_v, self.settings.smu_stop_v, self.settings.pre_scan_step_v):
                    self.check_stop()
                    vdc, idc, _ = session.set_smu_voltage_and_read_dc(smu_v)
                    self.log(f"Find start | SMU={smu_v:.4f} V | Vdc={vdc:.5e} V | Idc={idc:.5e} A")
                    if vdc >= self.settings.vdc_positive_threshold_v:
                        first_smu = smu_v
                        break

            if self.auto_smu_step_enabled():
                measured_voltage_points = self.adaptive_dc_sweep_rows(
                    session,
                    speed_name,
                    first_smu,
                    min(stop_smu, self.settings.smu_stop_v),
                    "CV",
                    stop_at_target_vpv=False,
                )
                smu_points = [row["smu_voltage_V"] for row in measured_voltage_points]
                target_vdc_step = self.target_vdc_step_for_speed(speed_name)
            else:
                measured_voltage_points = []
                smu_points = linear_points(first_smu, min(stop_smu, self.settings.smu_stop_v), self.settings.cv_smu_step_v)
                target_vdc_step = None
            self.log(
                f"Starting CV sweep with {len(smu_points)} voltage points, {len(freqs)} frequencies, "
                f"{self.settings.ac_samples_per_frequency} AC samples per frequency, "
                f"maximum impedance spread {self.settings.ac_max_impedance_spread_percent:g}%."
            )

            for sweep_index, smu_v in enumerate(smu_points, start=1):
                self.check_stop()
                vdc0, idc0, idc0_raw = session.set_smu_voltage_and_read_dc(smu_v)
                voltage_dc_quality = session.dc_quality_fields()
                negative_sample = session.negative_dc_sample()
                if self.settings.stop_if_idc_negative and negative_sample is not None:
                    negative_vdc, negative_idc, negative_idc_raw = negative_sample
                    negative_current_endpoint = NegativeCurrentEndpoint(
                        negative_vdc,
                        negative_idc,
                        negative_idc_raw,
                        freqs[0],
                        voltage_dc_quality,
                    )
                    cv_rows.append({
                        "timestamp": iso_now(),
                        "sweep_index": sweep_index,
                        "smu_voltage_V": smu_v,
                        "Vdc_pv_median_V": vdc0,
                        "Vdc_pv_mean_V": vdc0,
                        "Idc_pv_median_A": idc0,
                        "Pdc_pv_W": vdc0 * idc0,
                        **voltage_dc_quality,
                        "frequency_points_recorded": 0,
                        "cv_speed_level": speed_name,
                        "points_per_decade": level.points_per_decade,
                        "repeats_per_frequency": cv_repeats,
                        "ac_samples_per_frequency": self.settings.ac_samples_per_frequency,
                        "ac_max_impedance_spread_percent": self.settings.ac_max_impedance_spread_percent,
                        "ac_sample_interval_s": self.settings.ac_sample_interval_s,
                        "auto_smu_step_by_speed": self.auto_smu_step_enabled(),
                        "target_vdc_step_V": target_vdc_step if target_vdc_step is not None else "",
                        "measurement_endpoint": "negative_current",
                        "C_final_median_F": float("nan"),
                        "C_final_mean_F": float("nan"),
                        "C_final_std_F": float("nan"),
                        "C_final_method": "",
                        "filter_used_points": 0,
                        "filter_candidate_points": 0,
                        "filter_frequency_min_Hz": float("nan"),
                        "filter_frequency_max_Hz": float("nan"),
                    })
                    self.log(
                        "CV sweep reached its negative-current endpoint at the voltage point: "
                        f"Vdc={negative_vdc:.6e} V | Idc={negative_idc:.6e} A. Finishing normally."
                    )
                    break
                if self.settings.stop_if_vdc_exceeds_max and vdc0 > self.settings.max_vdc_pv_v:
                    self.log("CV sweep stopped because Vdc exceeded configured max.")
                    break
                self.log(f"CV voltage {sweep_index:>3}/{len(smu_points)} | SMU={smu_v:.6g} V | Vdc={vdc0:.6e} V | Idc={idc0:.6e} A")
                rows_for_voltage: List[Dict[str, Any]] = []
                total_freq_points = len(freqs) * cv_repeats
                count = 0
                for f_ac in freqs:
                    for repeat_index in range(1, cv_repeats + 1):
                        count += 1
                        base = {
                            "measurement_type": "CV",
                            "sweep_index": sweep_index,
                            "smu_voltage_V": smu_v,
                            "repeat_index": repeat_index,
                            "Vdc_pv_start_V": vdc0,
                            "Idc_pv_start_A": idc0,
                            "Idc_adc1_start_raw": idc0_raw,
                            "auto_smu_step_by_speed": self.auto_smu_step_enabled(),
                            "target_vdc_step_V": target_vdc_step if target_vdc_step is not None else "",
                        }
                        try:
                            row = self.measure_impedance_point(
                                session,
                                f_ac,
                                count,
                                total_freq_points,
                                base,
                                end_if_negative_idc=self.settings.stop_if_idc_negative,
                                rejected_rows=rejected_rows,
                                settling_s=settling_s,
                                remeasure_z_real_outliers=False,
                            )
                        except NegativeCurrentEndpoint as endpoint:
                            negative_current_endpoint = endpoint
                            voltage_dc_quality = dict(endpoint.quality)
                            self.log(
                                "CV sweep reached its negative-current endpoint during the "
                                f"frequency sweep: Vdc={endpoint.vdc:.6e} V | "
                                f"Idc={endpoint.idc:.6e} A | f={endpoint.frequency_hz:.6g} Hz. "
                                "Finishing with the measurements recorded so far."
                            )
                            break
                        if row is not None:
                            rows_for_voltage.append(row)
                            all_rows.append(row)
                    if negative_current_endpoint is not None:
                        break
                endpoint_vdc = negative_current_endpoint.vdc if negative_current_endpoint is not None else vdc0
                endpoint_idc = negative_current_endpoint.idc if negative_current_endpoint is not None else idc0

                filt = self.filter_capacitance_rows(rows_for_voltage)
                vdc_values = [r["Vdc_pv_V"] for r in rows_for_voltage if math.isfinite(r["Vdc_pv_V"])]
                idc_values = [r["Idc_pv_A"] for r in rows_for_voltage if math.isfinite(r["Idc_pv_A"])]
                cv_row: Dict[str, Any] = {
                    "timestamp": iso_now(),
                    "sweep_index": sweep_index,
                    "smu_voltage_V": smu_v,
                    "Vdc_pv_median_V": statistics.median(vdc_values) if vdc_values else endpoint_vdc,
                    "Vdc_pv_mean_V": sum(vdc_values) / len(vdc_values) if vdc_values else endpoint_vdc,
                    "Idc_pv_median_A": statistics.median(idc_values) if idc_values else endpoint_idc,
                    "Pdc_pv_W": (statistics.median(vdc_values) if vdc_values else endpoint_vdc) * (statistics.median(idc_values) if idc_values else endpoint_idc),
                    **voltage_dc_quality,
                    "frequency_points_recorded": len(rows_for_voltage),
                    "cv_speed_level": speed_name,
                    "points_per_decade": level.points_per_decade,
                    "repeats_per_frequency": cv_repeats,
                    "ac_samples_per_frequency": self.settings.ac_samples_per_frequency,
                    "ac_max_impedance_spread_percent": self.settings.ac_max_impedance_spread_percent,
                    "ac_sample_interval_s": self.settings.ac_sample_interval_s,
                    "auto_smu_step_by_speed": self.auto_smu_step_enabled(),
                    "target_vdc_step_V": target_vdc_step if target_vdc_step is not None else "",
                    "measurement_endpoint": "negative_current" if negative_current_endpoint is not None else "",
                }

                if filt:
                    cv_row.update({
                        "C_final_median_F": filt["final_median_F"],
                        "C_final_mean_F": filt["final_mean_F"],
                        "C_final_std_F": filt["final_std_F"],
                        "C_final_method": "filtered_frequency_median",
                        "filter_used_points": filt["used_count"],
                        "filter_candidate_points": filt["candidate_count"],
                        "filter_frequency_min_Hz": filt["frequency_min_Hz"],
                        "filter_frequency_max_Hz": filt["frequency_max_Hz"],
                    })
                else:
                    cv_row.update({
                        "C_final_median_F": float("nan"),
                        "C_final_mean_F": float("nan"),
                        "C_final_std_F": float("nan"),
                        "C_final_method": "",
                        "filter_used_points": 0,
                        "filter_candidate_points": 0,
                        "filter_frequency_min_Hz": float("nan"),
                        "filter_frequency_max_Hz": float("nan"),
                    })

                if filt:
                    final_capacitance = filt["final_median_F"]
                    final_method = "filtered_frequency_median"
                else:
                    final_capacitance = float("nan")
                    final_method = ""
                if math.isfinite(final_capacitance):
                    scale, unit_label = capacitance_scale_factor(self.settings.capacitance_unit)
                    self.log(
                        f"CV point saved | Vdc={cv_row['Vdc_pv_median_V']:.6e} V | "
                        f"C={final_capacitance * scale:.6g} {unit_label} | method={final_method}"
                    )
                else:
                    self.log("WARNING: No reliable capacitance value for this voltage point.")
                cv_rows.append(cv_row)

                save_rows(all_rows, output_dir / f"cv_frequency_sweeps_{timestamp}.csv")
                save_rows(cv_rows, output_dir / f"cv_curve_{timestamp}.csv")
                if rejected_rows:
                    save_rows(rejected_rows, output_dir / f"cv_rejected_impedance_outliers_{timestamp}.csv")
                if negative_current_endpoint is not None:
                    break

            detailed_csv = output_dir / f"cv_frequency_sweeps_{timestamp}.csv"
            cv_csv = output_dir / f"cv_curve_{timestamp}.csv"
            rejected_csv = output_dir / f"cv_rejected_impedance_outliers_{timestamp}.csv"
            save_rows(all_rows, detailed_csv)
            save_rows(cv_rows, cv_csv)
            files: List[Path] = []
            if all_rows:
                files.append(detailed_csv)
            if cv_rows:
                files.append(cv_csv)
            if rejected_rows:
                save_rows(rejected_rows, rejected_csv)
                files.append(rejected_csv)
            return RunResult(
                datasets={"cv_curve": cv_rows, "cv_frequency_sweeps": all_rows},
                output_files=files,
                summary={
                    "cv_speed_level": speed_name,
                    "negative_current_endpoint": negative_current_endpoint.as_dict() if negative_current_endpoint else {},
                },
            )
        finally:
            try:
                session.shutdown_outputs()
            finally:
                session.close()

    def dc_voltage_sweep_find_mpp(self, session: VisaController, smu_points: List[float], dc_rows: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], bool]:
        negative_endpoint = False
        self.log("Stage 1: DC voltage sweep to locate MPP. FG remains ON.")
        for idx, smu_v in enumerate(smu_points, start=1):
            self.check_stop()
            vdc, idc, idc_raw = session.set_smu_voltage_and_read_dc(smu_v)
            power = vdc * idc
            is_negative = idc < self.settings.negative_idc_limit_a
            is_candidate = (
                vdc >= self.settings.min_mpp_vdc_pv_v
                and idc >= self.settings.min_mpp_idc_pv_a
                and not is_negative
            )
            dc_rows.append({
                "timestamp": iso_now(),
                "point_index": idx,
                "smu_voltage_V": smu_v,
                "Vdc_pv_V": vdc,
                "Idc_adc1_raw": idc_raw,
                "Idc_pv_A": idc,
                "Pdc_pv_W": power,
                **session.dc_quality_fields(),
                "is_mpp_candidate": is_candidate,
                "is_negative_current_endpoint": is_negative,
            })
            self.log(f"MPP sweep {idx:>3}/{len(smu_points)} | SMU={smu_v:.6g} V | Vdc={vdc:.6e} V | Idc={idc:.6e} A | P={power:.6e} W")
            if is_negative:
                negative_endpoint = True
                break
        candidates = [row for row in dc_rows if row["is_mpp_candidate"]]
        if not candidates:
            raise RuntimeError("No usable MPP candidate was measured. Check current sign and sweep range.")
        mpp_row = max(candidates, key=lambda row: row["Pdc_pv_W"])
        self.log(f"Selected MPP | SMU={mpp_row['smu_voltage_V']:.6g} V | Vdc={mpp_row['Vdc_pv_V']:.6e} V | Idc={mpp_row['Idc_pv_A']:.6e} A | P={mpp_row['Pdc_pv_W']:.6e} W")
        return mpp_row, negative_endpoint

    def establish_manual_point(self, session: VisaController, dc_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        smu_v = self.settings.manual_smu_voltage_v
        vdc, idc, idc_raw = session.set_smu_voltage_and_read_dc(smu_v)
        row = {
            "timestamp": iso_now(),
            "point_index": 1,
            "smu_voltage_V": smu_v,
            "Vdc_pv_V": vdc,
            "Idc_adc1_raw": idc_raw,
            "Idc_pv_A": idc,
            "Pdc_pv_W": vdc * idc,
            **session.dc_quality_fields(),
            "is_mpp_candidate": False,
            "is_negative_current_endpoint": idc < self.settings.negative_idc_limit_a,
            "operating_point_mode": "MANUAL_SMU_VOLTAGE",
        }
        dc_rows.append(row)
        self.log(f"Manual point | SMU={smu_v:.6g} V | Vdc={vdc:.6e} V | Idc={idc:.6e} A | P={row['Pdc_pv_W']:.6e} W")
        return row

    def run_frequency_sweep(self, speed_name: str) -> RunResult:
        self.apply_ac_accuracy_for_speed(speed_name)
        self.validate()
        level = SPEED_LEVELS[speed_name]
        output_dir = self.settings.output_dir
        ensure_dir(output_dir)
        timestamp = now_tag()
        freqs = logspace_points(
            self.settings.freq_start_hz,
            self.settings.freq_stop_hz,
            level.points_per_decade,
            level.minimum_frequency_points,
        )
        settling_s = self.settings.settling_after_freq_s
        dc_rows: List[Dict[str, Any]] = []
        impedance_rows: List[Dict[str, Any]] = []
        rejected_rows: List[Dict[str, Any]] = []

        initial_smu = self.settings.smu_start_v if self.settings.operating_point_mode == "MPP_SEARCH" else self.settings.manual_smu_voltage_v
        session = VisaController(self.settings, self.log)
        try:
            session.open(need_dmm=True, need_lockin_i=True, need_lockin_v=True, need_fg=True, need_smu=True)
            session.configure_dmm()
            session.configure_fg(freqs[0])
            session.configure_led_fg()
            session.configure_smu(initial_smu)
            session.configure_lockins_for_impedance()
            time.sleep(settling_s)

            if self.settings.operating_point_mode == "MPP_SEARCH":
                if self.auto_smu_step_enabled():
                    measured_rows = self.cached_auto_smu_mpp_rows(
                        speed_name,
                        self.settings.smu_start_v,
                        self.settings.smu_stop_v,
                    )
                    reused_mpp_cache = bool(measured_rows)
                    if reused_mpp_cache:
                        self.log(
                            f"Reusing {len(measured_rows)} cached automatic SMU DC points for MPP. "
                            "Skipping voltage sweep and starting frequency sweep directly."
                        )
                    else:
                        measured_rows = self.adaptive_dc_sweep_rows(
                            session,
                            speed_name,
                            self.settings.smu_start_v,
                            self.settings.smu_stop_v,
                            "MPP",
                            stop_at_target_vpv=False,
                        )
                    for idx, measured in enumerate(measured_rows, start=1):
                        is_negative = measured["Idc_pv_A"] < self.settings.negative_idc_limit_a
                        is_candidate = (
                            measured["Vdc_pv_V"] >= self.settings.min_mpp_vdc_pv_v
                            and measured["Idc_pv_A"] >= self.settings.min_mpp_idc_pv_a
                            and not is_negative
                        )
                        dc_rows.append({
                            "timestamp": iso_now(),
                            "point_index": idx,
                            **measured,
                            "is_mpp_candidate": is_candidate,
                            "is_negative_current_endpoint": is_negative,
                            "auto_smu_step_by_speed": True,
                            "auto_smu_mpp_cache_reused": reused_mpp_cache,
                            "target_vdc_step_V": self.target_vdc_step_for_speed(speed_name),
                        })
                    candidates = [row for row in dc_rows if row["is_mpp_candidate"]]
                    if not candidates:
                        raise RuntimeError("No usable MPP candidate was measured. Check current sign and sweep range.")
                    operating_row = max(candidates, key=lambda row: row["Pdc_pv_W"])
                    self.log(
                        f"Selected MPP | SMU={operating_row['smu_voltage_V']:.6g} V | "
                        f"Vdc={operating_row['Vdc_pv_V']:.6e} V | "
                        f"Idc={operating_row['Idc_pv_A']:.6e} A | P={operating_row['Pdc_pv_W']:.6e} W"
                    )
                else:
                    smu_points = linear_points(self.settings.smu_start_v, self.settings.smu_stop_v, self.settings.smu_step_v)
                    operating_row, _ = self.dc_voltage_sweep_find_mpp(session, smu_points, dc_rows)
            else:
                operating_row = self.establish_manual_point(session, dc_rows)

            operating_smu = operating_row["smu_voltage_V"]
            session.set_smu_voltage_and_read_dc(operating_smu)
            self.log(
                f"Stage 2: Impedance frequency sweep at SMU={operating_smu:.6g} V | "
                f"{self.settings.ac_samples_per_frequency} AC samples per frequency | "
                f"maximum impedance spread {self.settings.ac_max_impedance_spread_percent:g}%."
            )

            negative_current_endpoint: Optional[NegativeCurrentEndpoint] = None
            for idx, f_ac in enumerate(freqs, start=1):
                base = {
                    "measurement_type": "FREQUENCY_SWEEP",
                    "operating_point_mode": self.settings.operating_point_mode,
                    "operating_point_smu_voltage_V": operating_smu,
                    "operating_point_reference_Vdc_pv_V": operating_row["Vdc_pv_V"],
                    "operating_point_reference_Idc_pv_A": operating_row["Idc_pv_A"],
                    "operating_point_reference_Pdc_pv_W": operating_row["Pdc_pv_W"],
                    "mpp_smu_voltage_V": operating_smu if self.settings.operating_point_mode == "MPP_SEARCH" else "",
                    "mpp_search_Vdc_pv_V": operating_row["Vdc_pv_V"] if self.settings.operating_point_mode == "MPP_SEARCH" else "",
                    "mpp_search_Idc_pv_A": operating_row["Idc_pv_A"] if self.settings.operating_point_mode == "MPP_SEARCH" else "",
                    "mpp_search_Pdc_pv_W": operating_row["Pdc_pv_W"] if self.settings.operating_point_mode == "MPP_SEARCH" else "",
                }
                try:
                    row = self.measure_impedance_point(
                        session,
                        f_ac,
                        idx,
                        len(freqs),
                        base,
                        end_if_negative_idc=self.settings.stop_if_idc_negative,
                        rejected_rows=rejected_rows,
                        settling_s=settling_s,
                    )
                except NegativeCurrentEndpoint as endpoint:
                    negative_current_endpoint = endpoint
                    dc_rows.append({
                        "timestamp": iso_now(),
                        "measurement_type": "FREQUENCY_SWEEP_ENDPOINT",
                        "smu_voltage_V": operating_smu,
                        **endpoint.as_dict(),
                        "is_negative_current_endpoint": True,
                    })
                    self.log(
                        "Frequency sweep reached its negative-current endpoint: "
                        f"Vdc={endpoint.vdc:.6e} V | Idc={endpoint.idc:.6e} A | "
                        f"f={endpoint.frequency_hz:.6g} Hz. Finishing with the measurements recorded so far."
                    )
                    break
                if row is not None:
                    impedance_rows.append(row)

            if not impedance_rows and negative_current_endpoint is None:
                raise RuntimeError("No valid impedance points were measured.")
            if self.settings.operating_point_mode == "MPP_SEARCH":
                self.log(f"MPP operating voltage was {operating_smu:.6g} V.")
            else:
                self.log(f"Manual operating voltage was {operating_smu:.6g} V.")
            self.log(f"After the run, SMU will be left ON at stop voltage: {self.settings.smu_stop_v:.6g} V")
            dc_csv = output_dir / f"frequency_dc_operating_point_{timestamp}.csv"
            imp_csv = output_dir / f"frequency_impedance_sweep_{timestamp}.csv"
            rej_csv = output_dir / f"frequency_rejected_impedance_outliers_{timestamp}.csv"
            save_rows(dc_rows, dc_csv)
            save_rows(impedance_rows, imp_csv)
            files = [dc_csv]
            if impedance_rows:
                files.append(imp_csv)
            if rejected_rows:
                save_rows(rejected_rows, rej_csv)
                files.append(rej_csv)
            return RunResult(
                datasets={"frequency_dc": dc_rows, "frequency_sweep": impedance_rows},
                output_files=files,
                summary={
                    "operating_point": operating_row,
                    "frequency_speed_level": speed_name,
                    "negative_current_endpoint": negative_current_endpoint.as_dict() if negative_current_endpoint else {},
                },
            )
        finally:
            try:
                session.shutdown_outputs()
            finally:
                session.close()

    def run_ab_monitor(self) -> RunResult:
        self.validate()
        output_dir = self.settings.output_dir
        ensure_dir(output_dir)
        timestamp = now_tag()
        rows: List[Dict[str, Any]] = []
        session = VisaController(self.settings, self.log)
        try:
            session.open(need_dmm=True, need_lockin_i=True, need_lockin_v=True, need_fg=True, need_smu=True)
            session.configure_dmm()
            session.configure_fg(self.settings.freq_start_hz)
            session.configure_led_fg()
            session.configure_smu(self.settings.manual_smu_voltage_v)
            session.configure_lockins_for_impedance()
            self.log("Starting A-B differential live monitor. Press Stop to end.")
            start = time.time()
            window = deque(maxlen=self.settings.ab_plot_window_points)
            point_index = 0
            active_smu_voltage = self.settings.manual_smu_voltage_v
            active_fg_frequency = self.settings.freq_start_hz
            active_led_duty = min(99.0, max(1.0, float(self.settings.led_duty_cycle_percent)))
            while not self.stop_event.is_set():
                point_index += 1
                t_s = time.time() - start
                controls = self.live_control_getter() if self.live_control_getter else {}
                requested_smu = controls.get("smu_voltage_v")
                requested_freq = controls.get("fg_frequency_hz")
                requested_led_duty = controls.get("led_duty_cycle_percent")
                if requested_smu is not None and float(requested_smu) != active_smu_voltage:
                    active_smu_voltage = float(requested_smu)
                    session.set_smu_voltage(active_smu_voltage)
                    self.log(f"Live monitor SMU voltage set to {active_smu_voltage:.6g} V")
                if requested_freq is not None and float(requested_freq) != active_fg_frequency:
                    active_fg_frequency = float(requested_freq)
                    session.strict_write(session.fg, f"FREQ {active_fg_frequency}", "Function generator")
                    self.log(f"Live monitor FG frequency set to {active_fg_frequency:.6g} Hz")
                if requested_led_duty is not None and float(requested_led_duty) != active_led_duty:
                    active_led_duty = min(99.0, max(1.0, float(requested_led_duty)))
                    session.set_led_duty_cycle(active_led_duty)
                    self.log(f"Live monitor LED brightness set to {active_led_duty:.3g}%")

                vdc_pv, idc_pv, idc_raw = session.read_dc(repeats=1)
                raw_v_value = session.query_float(session.lockin_v, "X.", "Lock-in 12 X")
                raw_v_phase = session.query_float(session.lockin_v, "PHA.", "Lock-in 12 phase")
                corrected_vpv = -raw_v_value
                corrected_phase = wrap_phase_deg(raw_v_phase + 180.0)
                current_value = session.query_float(session.lockin_i, "X.", "Lock-in 15 X")
                current_phase = session.query_float(session.lockin_i, "PHA.", "Lock-in 15 phase")
                cap_f = float("nan")
                z_mag = float("nan")
                z_phase = float("nan")
                z_real = float("nan")
                z_imag = float("nan")
                try:
                    live_vac_mag = abs(corrected_vpv)
                    live_vac_phase = corrected_phase
                    if self.settings.invert_voltage_phasor:
                        live_vac_mag, live_vac_phase = invert_phasor(live_vac_mag, live_vac_phase)
                    live_iac_mag, live_iac_phase = normalize_signed_phasor(
                        current_value * self.settings.iac_measurement_sign,
                        current_phase,
                    )
                    if self.settings.invert_current_phasor:
                        live_iac_mag, live_iac_phase = invert_phasor(live_iac_mag, live_iac_phase)
                    z_mag, z_phase, z_real, z_imag = impedance_from_mag_phase(
                        live_vac_mag,
                        live_vac_phase,
                        live_iac_mag,
                        live_iac_phase,
                        self.settings.min_iac_mag_a,
                    )
                    cap_f, _, _ = capacitance_from_impedance(z_real, z_imag, active_fg_frequency)
                except Exception as exc:
                    self.log(f"Live capacitance unavailable: {exc}")
                cap_scale, cap_unit = capacitance_scale_factor(self.settings.capacitance_unit)
                row = {
                    "timestamp": iso_now(),
                    "point_index": point_index,
                    "time_s": t_s,
                    "smu_voltage_V": active_smu_voltage,
                    "fg_frequency_Hz": active_fg_frequency,
                    "led_duty_cycle_percent": active_led_duty,
                    "Vpv_dc_V": vdc_pv,
                    "Idc_pv_A": idc_pv,
                    "Idc_adc1_raw": idc_raw,
                    "lockin12_raw_X_Vrms": raw_v_value,
                    "lockin12_raw_phase_deg": raw_v_phase,
                    "lockin12_corrected_Vpv_Vrms": corrected_vpv,
                    "lockin12_corrected_phase_deg": corrected_phase,
                    "lockin15_X_Vrms": current_value,
                    "lockin15_phase_deg": current_phase,
                    "live_Z_magnitude_ohm": z_mag,
                    "live_Z_phase_deg": z_phase,
                    "live_Z_real_ohm": z_real,
                    "live_Z_imag_ohm": z_imag,
                    "live_capacitance_F": cap_f,
                    f"live_capacitance_{cap_unit}": cap_f * cap_scale,
                }
                rows.append(row)
                window.append(row)
                self.log(f"A-B t={t_s:8.2f} s | Vpv_dc={vdc_pv:.6e} V | Vpv_ac={corrected_vpv:.6e} Vrms | phase={corrected_phase:8.3f} deg | Ipv_ac={current_value:.6e} Vrms | phase={current_phase:8.3f} deg | C={cap_f * cap_scale:.6g} {cap_unit}")
                if self.live_callback:
                    self.live_callback("ab_live", list(window))
                time.sleep(self.settings.ab_sample_interval_s)

            csv_path = output_dir / f"ab_differential_live_{timestamp}.csv"
            save_rows(rows, csv_path)
            self.log("A-B monitor stopped.")
            return RunResult(
                datasets={"ab_live": rows},
                output_files=[csv_path],
                summary={},
            )
        finally:
            try:
                session.shutdown_outputs()
            finally:
                session.close()

    def run_selected(self, selected: Dict[str, bool], speed_name: str) -> RunResult:
        self.sync_custom_speed_profile_from_settings()
        datasets: Dict[str, List[Dict[str, Any]]] = {}
        files: List[Path] = []
        summary: Dict[str, Any] = {}

        if selected.get("ab_live"):
            result = self.run_ab_monitor()
            datasets.update(result.datasets)
            files.extend(result.output_files)
            summary.update(result.summary)
            return RunResult(datasets, files, summary)

        if selected.get("iv_plot") or selected.get("pv_plot"):
            result = self.run_iv_pv(speed_name)
            datasets.update(result.datasets)
            files.extend(result.output_files)
            summary.update(result.summary)

        if selected.get("cv_plot"):
            result = self.run_cv(speed_name)
            datasets.update(result.datasets)
            files.extend(result.output_files)
            summary.update(result.summary)

        frequency_needed = any(
            selected.get(key)
            for key in [
                "z_real_plot",
                "z_imag_plot",
                "z_mag_plot",
                "z_phase_plot",
                "nyquist_plot",
                "cap_freq_plot",
            ]
        )
        if frequency_needed:
            result = self.run_frequency_sweep(speed_name)
            datasets.update(result.datasets)
            files.extend(result.output_files)
            summary.update(result.summary)

        if not datasets:
            raise RuntimeError("No plot or measurement type was selected.")
        return RunResult(datasets, files, summary)

