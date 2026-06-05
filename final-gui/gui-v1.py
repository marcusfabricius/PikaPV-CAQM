"""
PikaPV combined GUI

This file combines the IV/PV sweep, CV sweep, impedance frequency sweep,
and A-B differential live monitor into one GUI-driven measurement program.

Run:
    python gui-v1.py

Required packages:
    pip install pyvisa matplotlib

Notes:
    - PyVISA needs the correct VISA backend installed on the measurement PC.
    - The default GPIB addresses and commands are taken from the uploaded scripts.
    - Use Simulation mode only for checking the GUI without instruments.
"""

from __future__ import annotations

import csv
import math
import queue
import re
import statistics
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

try:
    import pyvisa  # type: ignore
except Exception:  # pragma: no cover
    pyvisa = None

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure


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


def capacitance_scale_factor(unit: str) -> Tuple[float, str]:
    unit_l = unit.lower().replace("µ", "u")
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
    repeats: int
    settling_multiplier: float


SPEED_LEVELS: Dict[str, SpeedLevel] = {
    "Custom": SpeedLevel("Custom", points_per_decade=8, minimum_frequency_points=8, repeats=2, settling_multiplier=1.0),
    "Fast": SpeedLevel("Fast", points_per_decade=4, minimum_frequency_points=6, repeats=1, settling_multiplier=1.0),
    "Medium": SpeedLevel("Medium", points_per_decade=8, minimum_frequency_points=10, repeats=2, settling_multiplier=1.0),
    "Slow": SpeedLevel("Slow", points_per_decade=16, minimum_frequency_points=16, repeats=4, settling_multiplier=1.0),
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
    custom_cv_vdc_pv_step_size_v: float = 0.025
    custom_cv_frequency_points_per_decade: int = 8
    custom_cv_minimum_frequency_points: int = 8
    custom_cv_settling_after_smu_s: float = 1.0
    custom_cv_settling_after_freq_s: float = 4.0
    custom_cv_lockin_time_constant_wait_s: float = 0.0
    custom_vdc_pv_step_size_v: float = 0.025
    custom_frequency_points_per_decade: int = 8
    custom_minimum_frequency_points: int = 8
    settling_after_smu_s: float = 1.0
    settling_after_freq_s: float = 4.0
    lockin_time_constant_wait_s: float = 0.0
    min_iac_mag_a: float = 1e-12

    # LED modulation function generator
    led_duty_cycle_percent: float = 50.0

    # Outlier handling
    remeasure_z_real_outliers: bool = True
    max_abs_z_real_ohm: float = 100.0
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

    def open(self, need_dmm=True, need_lockin_i=True, need_lockin_v=True, need_fg=True, need_smu=True, need_led_fg=True) -> None:
        if self.settings.simulation_mode:
            self.rm = FakeResourceManager()
            self.log("Simulation mode is ON. No hardware will be controlled.")
        else:
            if pyvisa is None:
                raise RuntimeError("pyvisa is not installed. Install it or use Simulation mode.")
            self.rm = pyvisa.ResourceManager()
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

    def read_dc(self) -> Tuple[float, float, float]:
        vdc_pv = self.query_float(self.dmm, "READ?", "DMM")
        idc_adc1_raw = self.query_float(self.lockin_i, self.settings.idc_adc1_cmd, "Lock-in current ADC1")
        idc_pv = idc_adc1_raw * self.settings.idc_adc1_to_ampere * self.settings.idc_measurement_sign
        self.check_dc_safety(vdc_pv, idc_pv)
        return vdc_pv, idc_pv, idc_adc1_raw

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
        if self.settings.max_outlier_retries < 0:
            raise ValueError("Maximum outlier retries must be zero or positive.")
        capacitance_scale_factor(self.settings.capacitance_unit)

    def auto_smu_step_enabled(self) -> bool:
        return bool(self.settings.auto_smu_range and self.settings.auto_smu_step_by_speed)

    def target_vdc_step_for_speed(self, speed_name: str) -> float:
        if speed_name == "Custom":
            return float(self.settings.custom_vdc_pv_step_size_v)
        return AUTO_VDC_STEP_BY_SPEED.get(speed_name, AUTO_VDC_STEP_BY_SPEED["Medium"])

    def sync_custom_speed_profile_from_settings(self) -> None:
        AUTO_VDC_STEP_BY_SPEED["Custom"] = float(self.settings.custom_vdc_pv_step_size_v)
        current = SPEED_LEVELS["Custom"]
        SPEED_LEVELS["Custom"] = SpeedLevel(
            "Custom",
            points_per_decade=max(1, int(self.settings.custom_frequency_points_per_decade)),
            minimum_frequency_points=max(1, int(self.settings.custom_minimum_frequency_points)),
            repeats=current.repeats,
            settling_multiplier=1.0,
        )

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
            per_freq_s = settle_freq + self.settings.lockin_time_constant_wait_s + 0.15
            total_s = pre_s + n_voltage * self.settings.settling_after_smu_s
            total_s += n_voltage * n_freq * level.repeats * per_freq_s
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
                )
                for idx, measured in enumerate(measured_rows, start=1):
                    rows.append({
                        "timestamp": iso_now(),
                        "point_index": idx,
                        **measured,
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
                        "auto_smu_step_by_speed": False,
                    })
                    self.log(f"IV {idx:>3}/{len(smu_points)} | SMU={smu_v:.4f} V | Vdc={vdc:.5e} V | Idc={idc:.5e} A | P={power:.5e} W")

                    if vdc >= self.settings.target_vpv_v:
                        self.log("IV sweep stopped because target Vpv was reached.")
                        break
                    if self.settings.stop_if_idc_negative and idc < self.settings.negative_idc_limit_a:
                        self.log("IV sweep stopped because Idc became negative.")
                        break

            if not rows:
                raise RuntimeError("No IV/PV points were recorded.")
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
        abort_if_negative_idc: bool,
        rejected_rows: List[Dict[str, Any]],
        settling_s: float,
    ) -> Optional[Dict[str, Any]]:
        session.strict_write(session.fg, f"FREQ {f_ac}", "Function generator")
        time.sleep(settling_s + self.settings.lockin_time_constant_wait_s)
        total_attempts = self.settings.max_outlier_retries + 1
        last_iac_mag = 0.0
        last_outlier_row: Optional[Dict[str, Any]] = None
        for attempt in range(1, total_attempts + 1):
            self.check_stop()
            vdc, idc, idc_raw = session.read_dc()
            if abort_if_negative_idc and idc < self.settings.negative_idc_limit_a:
                raise StopMeasurement(
                    f"Idc_pv became negative during impedance sweep: {idc:.6e} A < {self.settings.negative_idc_limit_a:.6e} A."
                )
            ph = session.read_ac_phasors()
            iac_mag = ph["Iac_mag_corrected_A"]
            last_iac_mag = iac_mag
            if iac_mag <= self.settings.min_iac_mag_a:
                self.log(f"Frequency {f_ac:.6g} Hz skipped, Iac too small.")
                return None

            z_mag, z_phase, z_real, z_imag = impedance_from_mag_phase(
                ph["Vac_mag_corrected_V"],
                ph["Vac_phase_corrected_deg"],
                ph["Iac_mag_corrected_A"],
                ph["Iac_phase_corrected_deg"],
                self.settings.min_iac_mag_a,
            )
            cap_f, y_real, y_imag = capacitance_from_impedance(z_real, z_imag, f_ac)
            is_outlier = self.settings.remeasure_z_real_outliers and abs(z_real) > self.settings.max_abs_z_real_ohm

            row = {
                **base_row,
                "timestamp": iso_now(),
                "point_index": point_index,
                "measurement_attempt": attempt,
                "outlier_retries_before_acceptance": attempt - 1,
                "is_rejected_Z_real_outlier": is_outlier,
                "Vdc_pv_V": vdc,
                "Idc_adc1_raw": idc_raw,
                "Idc_pv_A": idc,
                "Pdc_pv_W": vdc * idc,
                "f_ac_Hz": f_ac,
                "Vac_mag_raw_V": ph["Vac_mag_raw_V"],
                "Vac_phase_raw_deg": ph["Vac_phase_raw_deg"],
                "Vac_mag_corrected_V": ph["Vac_mag_corrected_V"],
                "Vac_phase_corrected_deg": ph["Vac_phase_corrected_deg"],
                "Iac_mag_raw_A": ph["Iac_mag_raw_A"],
                "Iac_phase_raw_deg": ph["Iac_phase_raw_deg"],
                "Iac_mag_corrected_A": ph["Iac_mag_corrected_A"],
                "Iac_phase_corrected_deg": ph["Iac_phase_corrected_deg"],
                "Z_real_ohm": z_real,
                "Z_imag_ohm": z_imag,
                "Z_magnitude_ohm": z_mag,
                "Z_mag_ohm": z_mag,
                "Z_phase_deg": z_phase,
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
                    f"C={cap_f * scale:.6g} {unit_label} | attempt={attempt}"
                )
                return row

            rejected_rows.append(row)
            last_outlier_row = row
            self.log(
                f"OUTLIER | f={f_ac:.6g} Hz | attempt={attempt}/{total_attempts} | "
                f"Z'={z_real:.6e} ohm exceeds +/-{self.settings.max_abs_z_real_ohm:g} ohm"
            )
            if attempt < total_attempts:
                time.sleep(self.settings.outlier_retry_wait_s + self.settings.lockin_time_constant_wait_s)

        if last_iac_mag > self.settings.min_iac_mag_a:
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
            self.log("WARNING: " + msg + " Point skipped.")
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
            self.log(f"Starting CV sweep with {len(smu_points)} voltage points, {len(freqs)} frequencies, {cv_repeats} pass per frequency.")

            for sweep_index, smu_v in enumerate(smu_points, start=1):
                self.check_stop()
                vdc0, idc0, idc0_raw = session.set_smu_voltage_and_read_dc(smu_v)
                if self.settings.stop_if_idc_negative and idc0 < self.settings.negative_idc_limit_a:
                    self.log("CV sweep stopped because Idc became negative at voltage point.")
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
                        row = self.measure_impedance_point(
                            session,
                            f_ac,
                            count,
                            total_freq_points,
                            base,
                            abort_if_negative_idc=self.settings.stop_if_idc_negative,
                            rejected_rows=rejected_rows,
                            settling_s=settling_s,
                        )
                        if row is not None:
                            rows_for_voltage.append(row)
                            all_rows.append(row)

                filt = self.filter_capacitance_rows(rows_for_voltage)
                vdc_values = [r["Vdc_pv_V"] for r in rows_for_voltage if math.isfinite(r["Vdc_pv_V"])]
                idc_values = [r["Idc_pv_A"] for r in rows_for_voltage if math.isfinite(r["Idc_pv_A"])]
                cv_row: Dict[str, Any] = {
                    "timestamp": iso_now(),
                    "sweep_index": sweep_index,
                    "smu_voltage_V": smu_v,
                    "Vdc_pv_median_V": statistics.median(vdc_values) if vdc_values else vdc0,
                    "Vdc_pv_mean_V": sum(vdc_values) / len(vdc_values) if vdc_values else vdc0,
                    "Idc_pv_median_A": statistics.median(idc_values) if idc_values else idc0,
                    "Pdc_pv_W": (statistics.median(vdc_values) if vdc_values else vdc0) * (statistics.median(idc_values) if idc_values else idc0),
                    "frequency_points_recorded": len(rows_for_voltage),
                    "cv_speed_level": speed_name,
                    "points_per_decade": level.points_per_decade,
                    "repeats_per_frequency": cv_repeats,
                    "auto_smu_step_by_speed": self.auto_smu_step_enabled(),
                    "target_vdc_step_V": target_vdc_step if target_vdc_step is not None else "",
                }
                if filt:
                    cv_row.update({
                        "C_final_median_F": filt["final_median_F"],
                        "C_final_mean_F": filt["final_mean_F"],
                        "C_final_std_F": filt["final_std_F"],
                        "filter_used_points": filt["used_count"],
                        "filter_candidate_points": filt["candidate_count"],
                        "filter_frequency_min_Hz": filt["frequency_min_Hz"],
                        "filter_frequency_max_Hz": filt["frequency_max_Hz"],
                    })
                    scale, unit_label = capacitance_scale_factor(self.settings.capacitance_unit)
                    self.log(f"CV point saved | Vdc={cv_row['Vdc_pv_median_V']:.6e} V | C={cv_row['C_final_median_F'] * scale:.6g} {unit_label}")
                else:
                    cv_row.update({
                        "C_final_median_F": float("nan"),
                        "C_final_mean_F": float("nan"),
                        "C_final_std_F": float("nan"),
                        "filter_used_points": 0,
                        "filter_candidate_points": 0,
                        "filter_frequency_min_Hz": float("nan"),
                        "filter_frequency_max_Hz": float("nan"),
                    })
                    self.log("WARNING: No reliable capacitance value for this voltage point.")
                cv_rows.append(cv_row)

                save_rows(all_rows, output_dir / f"cv_frequency_sweeps_{timestamp}.csv")
                save_rows(cv_rows, output_dir / f"cv_curve_{timestamp}.csv")
                if rejected_rows:
                    save_rows(rejected_rows, output_dir / f"cv_rejected_impedance_outliers_{timestamp}.csv")

            detailed_csv = output_dir / f"cv_frequency_sweeps_{timestamp}.csv"
            cv_csv = output_dir / f"cv_curve_{timestamp}.csv"
            rejected_csv = output_dir / f"cv_rejected_impedance_outliers_{timestamp}.csv"
            save_rows(all_rows, detailed_csv)
            save_rows(cv_rows, cv_csv)
            files = [detailed_csv, cv_csv]
            if rejected_rows:
                save_rows(rejected_rows, rejected_csv)
                files.append(rejected_csv)
            return RunResult(
                datasets={"cv_curve": cv_rows, "cv_frequency_sweeps": all_rows},
                output_files=files,
                summary={"cv_speed_level": speed_name},
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
            "is_mpp_candidate": False,
            "is_negative_current_endpoint": idc < self.settings.negative_idc_limit_a,
            "operating_point_mode": "MANUAL_SMU_VOLTAGE",
        }
        dc_rows.append(row)
        self.log(f"Manual point | SMU={smu_v:.6g} V | Vdc={vdc:.6e} V | Idc={idc:.6e} A | P={row['Pdc_pv_W']:.6e} W")
        return row

    def run_frequency_sweep(self, speed_name: str) -> RunResult:
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
            self.log(f"Stage 2: Impedance frequency sweep at SMU={operating_smu:.6g} V.")

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
                row = self.measure_impedance_point(
                    session,
                    f_ac,
                    idx,
                    len(freqs),
                    base,
                    abort_if_negative_idc=self.settings.operating_point_mode == "MPP_SEARCH",
                    rejected_rows=rejected_rows,
                    settling_s=settling_s,
                )
                if row is not None:
                    impedance_rows.append(row)

            if not impedance_rows:
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
            files = [dc_csv, imp_csv]
            if rejected_rows:
                save_rows(rejected_rows, rej_csv)
                files.append(rej_csv)
            return RunResult(
                datasets={"frequency_dc": dc_rows, "frequency_sweep": impedance_rows},
                output_files=files,
                summary={"operating_point": operating_row, "frequency_speed_level": speed_name},
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

                vdc_pv, idc_pv, idc_raw = session.read_dc()
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


# ============================================================================
# GUI
# ============================================================================


class PikaPVApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PikaPV")
        self.geometry("1500x920")
        self.minsize(1150, 740)

        self.msg_queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.engine: Optional[MeasurementEngine] = None
        self.datasets: Dict[str, List[Dict[str, Any]]] = {}
        self.last_result: Optional[RunResult] = None
        self.last_pre_scan_result: Optional[RunResult] = None

        self.settings_vars: Dict[str, tk.Variable] = {}
        self.plot_vars: Dict[str, tk.BooleanVar] = {}
        self.estimate_labels: Dict[str, ttk.Label] = {}

        self._build_gui()
        self._poll_queue()

    def _var(self, key: str, default: Any) -> tk.Variable:
        if isinstance(default, bool):
            var = tk.BooleanVar(value=default)
        elif isinstance(default, int):
            var = tk.StringVar(value=str(default))
        elif isinstance(default, float):
            var = tk.StringVar(value=f"{default:g}")
        else:
            var = tk.StringVar(value=str(default))
        self.settings_vars[key] = var
        return var

    def _build_gui(self) -> None:
        root = ttk.Frame(self, padding=8)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        left = ttk.Frame(root, width=360)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 8))
        left.grid_propagate(False)

        right = ttk.Frame(root)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        self._build_left_panel(left)
        self._build_right_panel(right)

    def _build_left_panel(self, parent: ttk.Frame) -> None:
        canvas = tk.Canvas(parent, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
        scroll_frame = ttk.Frame(canvas)
        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        title = ttk.Label(scroll_frame, text="Settings", font=("Segoe UI", 14, "bold"))
        title.pack(anchor="w", pady=(0, 8))

        plot_box = ttk.LabelFrame(scroll_frame, text="Choose plot type(s)", padding=8)
        plot_box.pack(fill=tk.X, pady=4)
        plot_options = [
            ("iv_plot", "IV plot"),
            ("pv_plot", "PV plot"),
            ("cv_plot", "CV plot"),
            ("z_real_plot", "Z' over frequency"),
            ("z_imag_plot", "Z'' over frequency"),
            ("z_mag_plot", "|Z| over frequency"),
            ("z_phase_plot", "Phase over frequency"),
            ("nyquist_plot", "Nyquist plot"),
            ("cap_freq_plot", "Capacitance over frequency"),
            ("ab_live", "A-B differential live monitor"),
        ]
        for key, label in plot_options:
            var = tk.BooleanVar(value=(key == "iv_plot"))
            self.plot_vars[key] = var
            ttk.Checkbutton(plot_box, text=label, variable=var).pack(anchor="w", pady=1)

        speed_box = ttk.LabelFrame(scroll_frame, text="Test speed", padding=8)
        speed_box.pack(fill=tk.X, pady=4)
        self.speed_var = tk.StringVar(value="Medium")
        for name in ["Custom", "Fast", "Medium", "Slow"]:
            level = SPEED_LEVELS[name]
            ttk.Radiobutton(
                speed_box,
                text=f"{name} - {level.points_per_decade} pts/dec, min {level.minimum_frequency_points}, {level.repeats} avg",
                variable=self.speed_var,
                value=name,
            ).pack(anchor="w")
            lbl = ttk.Label(speed_box, text="estimate: run pre-scan", foreground="gray")
            lbl.pack(anchor="w", padx=22)
            self.estimate_labels[name] = lbl

        axis_box = ttk.LabelFrame(scroll_frame, text="Custom plot", padding=8)
        axis_box.pack(fill=tk.X, pady=4)
        ttk.Label(axis_box, text="Dataset").pack(anchor="w")
        self.dataset_combo = ttk.Combobox(axis_box, state="readonly")
        self.dataset_combo.pack(fill=tk.X, pady=(0, 3))
        self.dataset_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_axis_options())
        ttk.Label(axis_box, text="X-axis").pack(anchor="w")
        self.x_combo = ttk.Combobox(axis_box, state="readonly")
        self.x_combo.pack(fill=tk.X, pady=(0, 3))
        ttk.Label(axis_box, text="Y-axis").pack(anchor="w")
        self.y_combo = ttk.Combobox(axis_box, state="readonly")
        self.y_combo.pack(fill=tk.X, pady=(0, 3))
        self.x_scale_var = tk.StringVar(value="linear")
        self.y_scale_var = tk.StringVar(value="linear")
        scale_frame = ttk.Frame(axis_box)
        scale_frame.pack(fill=tk.X, pady=3)
        ttk.Label(scale_frame, text="X scale").grid(row=0, column=0, sticky="w")
        ttk.Combobox(scale_frame, textvariable=self.x_scale_var, values=["linear", "log"], state="readonly", width=8).grid(row=0, column=1, sticky="w", padx=5)
        ttk.Label(scale_frame, text="Y scale").grid(row=1, column=0, sticky="w")
        ttk.Combobox(scale_frame, textvariable=self.y_scale_var, values=["linear", "log"], state="readonly", width=8).grid(row=1, column=1, sticky="w", padx=5)
        ttk.Button(axis_box, text="Plot custom X/Y", command=self.plot_custom).pack(fill=tk.X, pady=(6, 0))

        sweep_box = ttk.LabelFrame(scroll_frame, text="Sweep settings", padding=8)
        sweep_box.pack(fill=tk.X, pady=4)
        self._entry(sweep_box, "SMU start [V]", "smu_start_v", 11.0)
        self._entry(sweep_box, "SMU stop [V]", "smu_stop_v", 15.0)
        self._entry(sweep_box, "IV/MPP SMU step [V]", "smu_step_v", 0.05)
        self._entry(sweep_box, "CV SMU step [V]", "cv_smu_step_v", 0.01)
        self._entry(sweep_box, "Pre-scan step [V]", "pre_scan_step_v", 0.10)
        self._entry(sweep_box, "Target Vpv for IV [V]", "target_vpv_v", 1.0)
        self._entry(sweep_box, "Max SMU voltage [V]", "max_smu_v", 15.0)
        self._entry(sweep_box, "Max Vdc_pv [V]", "max_vdc_pv_v", 0.80)
        self._entry(sweep_box, "Max |Idc| safety [A]", "max_idc_abs_a", 10.0)
        self._entry(sweep_box, "Idc ADC1 to ampere", "idc_adc1_to_ampere", 1.0)
        self._entry(sweep_box, "Idc sign (+1 or -1)", "idc_measurement_sign", 1.0)
        self._check(sweep_box, "Stop when Vdc exceeds max", "stop_if_vdc_exceeds_max", False)
        self._check(sweep_box, "Stop when |Idc| exceeds max", "stop_if_idc_abs_exceeds_max", True)
        self._check(sweep_box, "Stop when Idc becomes negative", "stop_if_idc_negative", True)

        freq_box = ttk.LabelFrame(scroll_frame, text="AC and frequency settings", padding=8)
        freq_box.pack(fill=tk.X, pady=4)
        self._entry(freq_box, "Frequency start [Hz]", "freq_start_hz", 5.0)
        self._entry(freq_box, "Frequency stop [Hz]", "freq_stop_hz", 10000.0)
        self._entry(freq_box, "AC perturbation [Vpp]", "vac_vpp", 0.010)
        self._entry(freq_box, "Custom Vdc_pv step [V]", "custom_vdc_pv_step_size_v", 0.025)
        self._entry(freq_box, "Custom frequency points/decade", "custom_frequency_points_per_decade", 8)
        self._entry(freq_box, "Custom minimum frequency points", "custom_minimum_frequency_points", 8)
        self._entry(freq_box, "Settle after freq [s]", "settling_after_freq_s", 4.0)
        self._entry(freq_box, "Settle after SMU [s]", "settling_after_smu_s", 1.0)
        self._entry(freq_box, "Max |Z'| before retry [ohm]", "max_abs_z_real_ohm", 100.0)
        self._entry(freq_box, "Outlier retries", "max_outlier_retries", 8)
        self._entry(freq_box, "Iac sign (+1 or -1)", "iac_measurement_sign", -1.0)
        self._entry(freq_box, "Nyquist Y sign (+1 or -1)", "nyquist_y_axis_sign", 1.0)
        self._check(freq_box, "Remeasure Z' outliers", "remeasure_z_real_outliers", True)
        self._check(freq_box, "Abort if Z' retries fail", "abort_if_outlier_retries_exhausted", False)
        self._check(freq_box, "Invert current phasor", "invert_current_phasor", False)
        self._check(freq_box, "Invert voltage phasor", "invert_voltage_phasor", False)

        op_box = ttk.LabelFrame(scroll_frame, text="Frequency operating point", padding=8)
        op_box.pack(fill=tk.X, pady=4)
        self.operating_point_var = tk.StringVar(value="MPP_SEARCH")
        ttk.Radiobutton(op_box, text="Find MPP first", variable=self.operating_point_var, value="MPP_SEARCH").pack(anchor="w")
        ttk.Radiobutton(op_box, text="Use manual SMU voltage", variable=self.operating_point_var, value="MANUAL_SMU_VOLTAGE").pack(anchor="w")
        self._entry(op_box, "Manual SMU voltage [V]", "manual_smu_voltage_v", 12.5)

        visa_box = ttk.LabelFrame(scroll_frame, text="VISA addresses", padding=8)
        visa_box.pack(fill=tk.X, pady=4)
        self._entry(visa_box, "DMM", "dmm_addr", "GPIB0::10::INSTR")
        self._entry(visa_box, "Lock-in current", "lockin_i_addr", "GPIB0::15::INSTR")
        self._entry(visa_box, "Lock-in voltage A-B", "lockin_v_addr", "GPIB0::12::INSTR")
        self._entry(visa_box, "Function generator", "fg_addr", "GPIB0::14::INSTR")
        self._entry(visa_box, "SMU", "smu_addr", "GPIB0::26::INSTR")
        self._check(visa_box, "Configure lock-ins on start", "configure_lockins", True)
        self._check(visa_box, "Simulation mode", "simulation_mode", False)

        output_box = ttk.LabelFrame(scroll_frame, text="Output", padding=8)
        output_box.pack(fill=tk.X, pady=4)
        out_frame = ttk.Frame(output_box)
        out_frame.pack(fill=tk.X)
        self.output_dir_var = tk.StringVar(value="measurement_output")
        ttk.Entry(out_frame, textvariable=self.output_dir_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(out_frame, text="Browse", command=self.browse_output_dir).pack(side=tk.LEFT, padx=(4, 0))

        button_box = ttk.Frame(scroll_frame)
        button_box.pack(fill=tk.X, pady=8)
        self.pre_scan_button = ttk.Button(button_box, text="Pre-scan + estimate", command=self.start_pre_scan)
        self.pre_scan_button.pack(fill=tk.X, pady=2)
        self.start_button = ttk.Button(button_box, text="Start measurement", command=self.start_measurement)
        self.start_button.pack(fill=tk.X, pady=2)
        self.stop_button = ttk.Button(button_box, text="Stop", command=self.stop_measurement, state=tk.DISABLED)
        self.stop_button.pack(fill=tk.X, pady=2)
        ttk.Button(button_box, text="Save current plot as PNG", command=self.save_current_plot).pack(fill=tk.X, pady=2)

    def _entry(self, parent: ttk.Frame, label: str, key: str, default: Any) -> None:
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.X, pady=2)
        ttk.Label(frame, text=label).pack(anchor="w")
        ttk.Entry(frame, textvariable=self._var(key, default)).pack(fill=tk.X)

    def _check(self, parent: ttk.Frame, label: str, key: str, default: bool) -> None:
        ttk.Checkbutton(parent, text=label, variable=self._var(key, default)).pack(anchor="w", pady=1)

    def _build_right_panel(self, parent: ttk.Frame) -> None:
        notebook = ttk.Notebook(parent)
        notebook.grid(row=0, column=0, sticky="nsew")
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)

        plot_tab = ttk.Frame(notebook)
        log_tab = ttk.Frame(notebook)
        notebook.add(plot_tab, text="Plots")
        notebook.add(log_tab, text="Log")

        plot_tab.rowconfigure(0, weight=1)
        plot_tab.columnconfigure(0, weight=1)
        self.figure = Figure(figsize=(9, 6), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.figure, master=plot_tab)
        self.canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        toolbar_frame = ttk.Frame(plot_tab)
        toolbar_frame.grid(row=1, column=0, sticky="ew")
        NavigationToolbar2Tk(self.canvas, toolbar_frame)

        log_tab.rowconfigure(0, weight=1)
        log_tab.columnconfigure(0, weight=1)
        self.log_text = ScrolledText(log_tab, wrap=tk.WORD, height=20)
        self.log_text.grid(row=0, column=0, sticky="nsew")

    def browse_output_dir(self) -> None:
        path = filedialog.askdirectory(initialdir=self.output_dir_var.get() or ".")
        if path:
            self.output_dir_var.set(path)

    def log(self, msg: str) -> None:
        self.msg_queue.put(("log", msg))

    def live_callback(self, name: str, rows: List[Dict[str, Any]]) -> None:
        self.msg_queue.put(("live", name, rows))

    def read_settings(self) -> Settings:
        def get_str(key: str) -> str:
            return str(self.settings_vars[key].get()).strip()

        def get_float(key: str) -> float:
            return float(get_str(key))

        def get_int(key: str) -> int:
            return int(float(get_str(key)))

        def get_bool(key: str) -> bool:
            return bool(self.settings_vars[key].get())

        return Settings(
            dmm_addr=get_str("dmm_addr"),
            lockin_i_addr=get_str("lockin_i_addr"),
            lockin_v_addr=get_str("lockin_v_addr"),
            fg_addr=get_str("fg_addr"),
            smu_addr=get_str("smu_addr"),
            output_dir=Path(self.output_dir_var.get()).expanduser(),
            simulation_mode=get_bool("simulation_mode"),
            smu_start_v=get_float("smu_start_v"),
            smu_stop_v=get_float("smu_stop_v"),
            smu_step_v=get_float("smu_step_v"),
            cv_smu_step_v=get_float("cv_smu_step_v"),
            pre_scan_step_v=get_float("pre_scan_step_v"),
            max_smu_v=get_float("max_smu_v"),
            target_vpv_v=get_float("target_vpv_v"),
            max_vdc_pv_v=get_float("max_vdc_pv_v"),
            max_idc_abs_a=get_float("max_idc_abs_a"),
            idc_adc1_to_ampere=get_float("idc_adc1_to_ampere"),
            idc_measurement_sign=get_float("idc_measurement_sign"),
            stop_if_vdc_exceeds_max=get_bool("stop_if_vdc_exceeds_max"),
            stop_if_idc_abs_exceeds_max=get_bool("stop_if_idc_abs_exceeds_max"),
            stop_if_idc_negative=get_bool("stop_if_idc_negative"),
            freq_start_hz=get_float("freq_start_hz"),
            freq_stop_hz=get_float("freq_stop_hz"),
            vac_vpp=get_float("vac_vpp"),
            custom_vdc_pv_step_size_v=get_float("custom_vdc_pv_step_size_v"),
            custom_frequency_points_per_decade=get_int("custom_frequency_points_per_decade"),
            custom_minimum_frequency_points=get_int("custom_minimum_frequency_points"),
            settling_after_freq_s=get_float("settling_after_freq_s"),
            settling_after_smu_s=get_float("settling_after_smu_s"),
            max_abs_z_real_ohm=get_float("max_abs_z_real_ohm"),
            max_outlier_retries=get_int("max_outlier_retries"),
            remeasure_z_real_outliers=get_bool("remeasure_z_real_outliers"),
            abort_if_outlier_retries_exhausted=get_bool("abort_if_outlier_retries_exhausted"),
            iac_measurement_sign=get_float("iac_measurement_sign"),
            nyquist_y_axis_sign=get_float("nyquist_y_axis_sign"),
            invert_current_phasor=get_bool("invert_current_phasor"),
            invert_voltage_phasor=get_bool("invert_voltage_phasor"),
            operating_point_mode=self.operating_point_var.get(),
            manual_smu_voltage_v=get_float("manual_smu_voltage_v"),
            configure_lockins=get_bool("configure_lockins"),
        )

    def set_running(self, running: bool) -> None:
        self.start_button.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.pre_scan_button.configure(state=tk.DISABLED if running else tk.NORMAL)
        self.stop_button.configure(state=tk.NORMAL if running else tk.DISABLED)

    def start_pre_scan(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            settings = self.read_settings()
        except Exception as exc:
            messagebox.showerror("Settings error", str(exc))
            return
        self.stop_event.clear()
        self.set_running(True)
        self.log_text.delete("1.0", tk.END)
        self.engine = MeasurementEngine(settings, self.log, self.stop_event, self.live_callback)

        def task() -> None:
            try:
                result = self.engine.run_pre_scan()
                self.msg_queue.put(("pre_scan_done", result))
            except UserStop as exc:
                self.msg_queue.put(("stopped", str(exc)))
            except StopMeasurement as exc:
                self.msg_queue.put(("stopped", "Measurement stopped by safety limit:\n" + str(exc)))
            except Exception:
                self.msg_queue.put(("error", traceback.format_exc()))

        self.worker = threading.Thread(target=task, daemon=True)
        self.worker.start()

    def start_measurement(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            settings = self.read_settings()
        except Exception as exc:
            messagebox.showerror("Settings error", str(exc))
            return
        selected = {key: var.get() for key, var in self.plot_vars.items()}
        speed = self.speed_var.get()
        self.stop_event.clear()
        self.set_running(True)
        self.log_text.delete("1.0", tk.END)
        self.datasets = {}
        self.last_result = None
        self.engine = MeasurementEngine(settings, self.log, self.stop_event, self.live_callback)
        if self.last_pre_scan_result and "pre_scan" in self.last_pre_scan_result.summary:
            self.engine.last_pre_scan = self.last_pre_scan_result.summary["pre_scan"]

        def task() -> None:
            try:
                result = self.engine.run_selected(selected, speed)
                self.msg_queue.put(("done", result))
            except UserStop as exc:
                self.msg_queue.put(("stopped", str(exc)))
            except StopMeasurement as exc:
                self.msg_queue.put(("stopped", "Measurement stopped by safety limit:\n" + str(exc)))
            except Exception:
                self.msg_queue.put(("error", traceback.format_exc()))

        self.worker = threading.Thread(target=task, daemon=True)
        self.worker.start()

    def stop_measurement(self) -> None:
        self.stop_event.set()
        self.log("Stop requested. Waiting for safe shutdown...")

    def _poll_queue(self) -> None:
        try:
            while True:
                item = self.msg_queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append_log(item[1])
                elif kind == "live":
                    _, name, rows = item
                    self.datasets[name] = rows
                    self.plot_ab_live(rows)
                elif kind == "pre_scan_done":
                    result = item[1]
                    self.last_pre_scan_result = result
                    self.datasets.update(result.datasets)
                    self.last_result = result
                    self._append_log("Pre-scan saved files:")
                    for path in result.output_files:
                        self._append_log(f"  {path}")
                    estimates = result.summary.get("cv_duration_estimates", {})
                    for name, text in estimates.items():
                        self.estimate_labels[name].configure(text=f"estimate: {text}")
                    self.update_dataset_controls()
                    self.plot_builtin()
                    self.set_running(False)
                elif kind == "done":
                    result = item[1]
                    self.last_result = result
                    self.datasets.update(result.datasets)
                    self._append_log("Measurement saved files:")
                    for path in result.output_files:
                        self._append_log(f"  {path}")
                    self.update_dataset_controls()
                    self.plot_builtin()
                    self.set_running(False)
                elif kind == "stopped":
                    self._append_log(item[1])
                    self.set_running(False)
                elif kind == "error":
                    self._append_log("ERROR:")
                    self._append_log(item[1])
                    self.set_running(False)
                    messagebox.showerror("Measurement error", item[1])
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _append_log(self, msg: str) -> None:
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)

    def selected_plots(self) -> Dict[str, bool]:
        return {key: var.get() for key, var in self.plot_vars.items()}

    def update_dataset_controls(self) -> None:
        names = list(self.datasets.keys())
        self.dataset_combo.configure(values=names)
        if names and not self.dataset_combo.get():
            self.dataset_combo.set(names[0])
        self._refresh_axis_options()

    def _refresh_axis_options(self) -> None:
        name = self.dataset_combo.get()
        rows = self.datasets.get(name, [])
        if not rows:
            return
        columns = union_fieldnames(rows)
        numeric_cols = []
        for col in columns:
            for row in rows[:30]:
                if safe_log_value(row.get(col)) is not None:
                    numeric_cols.append(col)
                    break
        self.x_combo.configure(values=numeric_cols)
        self.y_combo.configure(values=numeric_cols)
        if numeric_cols:
            if self.x_combo.get() not in numeric_cols:
                self.x_combo.set(numeric_cols[0])
            if self.y_combo.get() not in numeric_cols:
                self.y_combo.set(numeric_cols[min(1, len(numeric_cols) - 1)])

    def get_dataset_for_frequency(self) -> List[Dict[str, Any]]:
        if self.datasets.get("frequency_sweep"):
            return self.datasets["frequency_sweep"]
        if self.datasets.get("cv_frequency_sweeps"):
            return self.datasets["cv_frequency_sweeps"]
        return []

    def plot_builtin(self) -> None:
        selected = self.selected_plots()
        plot_specs = []
        if selected.get("iv_plot") and self.datasets.get("iv_pv_sweep"):
            plot_specs.append(("iv_pv_sweep", "Vdc_pv_V", "Idc_pv_A", "IV curve", "Vdc_pv [V]", "Idc_pv [A]", False))
        if selected.get("pv_plot") and self.datasets.get("iv_pv_sweep"):
            plot_specs.append(("iv_pv_sweep", "Vdc_pv_V", "Pdc_pv_W", "PV curve", "Vdc_pv [V]", "Power [W]", False))
        if selected.get("cv_plot") and self.datasets.get("cv_curve"):
            plot_specs.append(("cv_curve", "Vdc_pv_median_V", "C_final_median_F", "CV curve", "Vdc_pv [V]", f"Capacitance [{self.settings_or_default().capacitance_unit}]", False))

        freq_rows = self.get_dataset_for_frequency()
        if freq_rows:
            if selected.get("z_real_plot"):
                plot_specs.append(("_frequency", "f_ac_Hz", "Z_real_ohm", "Z' over frequency", "Frequency [Hz]", "Z' [ohm]", True))
            if selected.get("z_imag_plot"):
                plot_specs.append(("_frequency", "f_ac_Hz", "Z_imag_ohm", "Z'' over frequency", "Frequency [Hz]", "Z'' [ohm]", True))
            if selected.get("z_mag_plot"):
                plot_specs.append(("_frequency", "f_ac_Hz", "Z_magnitude_ohm", "|Z| over frequency", "Frequency [Hz]", "|Z| [ohm]", True))
            if selected.get("z_phase_plot"):
                plot_specs.append(("_frequency", "f_ac_Hz", "Z_phase_deg", "Phase over frequency", "Frequency [Hz]", "Phase [deg]", True))
            if selected.get("cap_freq_plot"):
                plot_specs.append(("_frequency", "f_ac_Hz", "C_uncorrected_F", "Capacitance over frequency", "Frequency [Hz]", f"Capacitance [{self.settings_or_default().capacitance_unit}]", True))
            if selected.get("nyquist_plot"):
                y_label = "-Z'' [ohm]" if self.settings_or_default().nyquist_y_axis_sign < 0 else "Z'' [ohm]"
                plot_specs.append(("_frequency", "Z_real_ohm", "Z_imag_ohm", "Nyquist plot", "Z' [ohm]", y_label, False, True))

        if selected.get("ab_live") and self.datasets.get("ab_live"):
            self.plot_ab_live(self.datasets["ab_live"])
            return

        if not plot_specs:
            self.figure.clear()
            ax = self.figure.add_subplot(111)
            ax.text(0.5, 0.5, "No data to plot yet", ha="center", va="center")
            ax.set_axis_off()
            self.canvas.draw_idle()
            return

        self.figure.clear()
        n = len(plot_specs)
        ncols = 2 if n > 1 else 1
        nrows = math.ceil(n / ncols)
        axes = self.figure.subplots(nrows, ncols, squeeze=False)
        for ax in axes.flat[n:]:
            ax.set_visible(False)

        for ax, spec in zip(axes.flat, plot_specs):
            dataset_name, x_key, y_key, title, xlabel, ylabel, force_log_x, *extra = spec
            is_nyquist = bool(extra and extra[0])
            if dataset_name == "_frequency":
                rows = freq_rows
            else:
                rows = self.datasets.get(dataset_name, [])
            x, y = self.extract_xy(rows, x_key, y_key)
            if is_nyquist:
                y = [value * self.settings_or_default().nyquist_y_axis_sign for value in y]
            if y_key in {"C_final_median_F", "C_uncorrected_F"}:
                scale, unit_label = capacitance_scale_factor(self.settings_or_default().capacitance_unit)
                y = [v * scale for v in y]
                ylabel = ylabel.replace(self.settings_or_default().capacitance_unit, unit_label)
            ax.plot(x, y, marker="o", linestyle="-")
            ax.set_title(title)
            ax.set_xlabel(xlabel)
            ax.set_ylabel(ylabel)
            ax.grid(True, which="both")
            if force_log_x or self.x_scale_var.get() == "log":
                if all(v > 0 for v in x):
                    ax.set_xscale("log")
            if self.y_scale_var.get() == "log" and all(v > 0 for v in y):
                ax.set_yscale("log")
        self.figure.tight_layout()
        self.canvas.draw_idle()

    def settings_or_default(self) -> Settings:
        try:
            return self.read_settings()
        except Exception:
            return Settings()

    def extract_xy(self, rows: List[Dict[str, Any]], x_key: str, y_key: str) -> Tuple[List[float], List[float]]:
        x: List[float] = []
        y: List[float] = []
        for row in rows:
            xv = safe_log_value(row.get(x_key))
            yv = safe_log_value(row.get(y_key))
            if xv is None or yv is None:
                continue
            x.append(xv)
            y.append(yv)
        return x, y

    def plot_custom(self) -> None:
        name = self.dataset_combo.get()
        x_key = self.x_combo.get()
        y_key = self.y_combo.get()
        rows = self.datasets.get(name, [])
        if not rows or not x_key or not y_key:
            messagebox.showinfo("Custom plot", "Choose a dataset and X/Y columns first.")
            return
        x, y = self.extract_xy(rows, x_key, y_key)
        if not x:
            messagebox.showinfo("Custom plot", "No numeric data found for this X/Y combination.")
            return
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        ax.plot(x, y, marker="o", linestyle="-")
        ax.set_title(f"{name}: {y_key} vs {x_key}")
        ax.set_xlabel(x_key)
        ax.set_ylabel(y_key)
        ax.grid(True, which="both")
        if self.x_scale_var.get() == "log" and all(v > 0 for v in x):
            ax.set_xscale("log")
        if self.y_scale_var.get() == "log" and all(v > 0 for v in y):
            ax.set_yscale("log")
        self.figure.tight_layout()
        self.canvas.draw_idle()

    def plot_ab_live(self, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        self.figure.clear()
        ax1 = self.figure.add_subplot(211)
        ax2 = self.figure.add_subplot(212, sharex=ax1)
        t, vpv = self.extract_xy(rows, "time_s", "lockin12_corrected_Vpv_Vrms")
        _, lia15 = self.extract_xy(rows, "time_s", "lockin15_X_Vrms")
        _, ph12 = self.extract_xy(rows, "time_s", "lockin12_corrected_phase_deg")
        _, ph15 = self.extract_xy(rows, "time_s", "lockin15_phase_deg")
        if t and vpv:
            ax1.plot(t[: len(vpv)], vpv, marker=".", linestyle="-", label="Lock-in 12 corrected Vpv")
        if t and lia15:
            ax1.plot(t[: len(lia15)], lia15, marker=".", linestyle="-", label="Lock-in 15 A input")
        if t and ph12:
            ax2.plot(t[: len(ph12)], ph12, marker=".", linestyle="-", label="Lock-in 12 phase")
        if t and ph15:
            ax2.plot(t[: len(ph15)], ph15, marker=".", linestyle="-", label="Lock-in 15 phase")
        ax1.set_ylabel("Signed value [V RMS]")
        ax2.set_ylabel("Phase [deg]")
        ax2.set_xlabel("Time [s]")
        ax1.set_title("A-B differential live monitor")
        ax1.grid(True)
        ax2.grid(True)
        ax1.legend(loc="best")
        ax2.legend(loc="best")
        self.figure.tight_layout()
        self.canvas.draw_idle()

    def save_current_plot(self) -> None:
        default = Path(self.output_dir_var.get() or ".") / f"pikapv_plot_{now_tag()}.png"
        path = filedialog.asksaveasfilename(
            initialfile=default.name,
            initialdir=str(default.parent),
            defaultextension=".png",
            filetypes=[("PNG image", "*.png"), ("All files", "*.*")],
        )
        if path:
            self.figure.savefig(path, dpi=300)
            self._append_log(f"Saved current plot: {path}")


if __name__ == "__main__":
    app = PikaPVApp()
    app.mainloop()
