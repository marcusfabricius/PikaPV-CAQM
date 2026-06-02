import pyvisa
import time
import csv
import math
import re
import statistics
import matplotlib.pyplot as plt
from datetime import datetime
from pathlib import Path

# ============================================================
# CV CURVE MEASUREMENT USING SELECTABLE SWEEP ORDER
# ============================================================
#
# Instruments:
#   DMM          GPIB0::10::INSTR   measures Vdc_pv
#   Lock-in #1   GPIB0::15::INSTR   measures Iac_pv on input A + ADC1 for Idc_pv
#   Lock-in #2   GPIB0::12::INSTR   measures Vac_pv on A-B differential
#   Function gen GPIB0::14::INSTR   provides AC perturbation
#   SMU          GPIB0::26::INSTR   sets DC operating point
#
# Measurement logic:
#   1. Optionally do a DC voltage pre-scan with the function generator held
#      static, for example 5 kHz and 10 mVpp.
#   2. Use the pre-scan to find the first positive measured Vdc_pv point and
#      the last voltage point before Idc_pv becomes negative.
#   3. Estimate and print the expected capacitance measurement time.
#   4. Create the frequency list using either logarithmic or linear spacing.
#   5. Run the main capacitance measurement in the selected order:
#        - set one frequency and sweep all voltages, or
#        - set one voltage and sweep all frequencies.
#   6. Only after all tests are complete, optionally discard high-voltage
#      capacitance drops at each fixed frequency.
#   7. Group data by voltage point.
#   8. Calculate one filtered capacitance per voltage and plot C and Cj versus Vdc_pv.
# ============================================================


# ============================================================
# USER SETTINGS
# ============================================================

# SMU voltage sweep settings
SMU_SWEEP_START_VOLTAGE = 12.0      # [V]
SMU_SWEEP_STOP_VOLTAGE = 16.0      # [V]
SMU_SWEEP_STEP_VOLTAGE = 0.5      # [V]
MAX_SMU_VOLTAGE = 16.0             # [V]

# First positive measured PV voltage threshold
VDC_POSITIVE_THRESHOLD = 1e-4       # [V]

# Stop if measured PV DC voltage becomes too high
MAX_VDC_PV = 7.00                  # [V]
STOP_IF_VDC_EXCEEDS_MAX = True

# SMU current compliance / current limit
SMU_CURRENT_LIMIT_A = 0.5           # [A]

# Function generator AC signal
VAC_VPP = 0.010                    # [Vpp] 10 mVpp
FG_OFFSET = 0.0                    # [V]
FG_WAVEFORM = "SIN"                # sine wave

# Frequencies to test. For each frequency, the script performs a full voltage sweep.
F_START = 100                      # [Hz]
F_STOP = 10000.0                  # [Hz]

# Frequency spacing mode.
# Options:
#   "log"    -> logarithmic spacing using POINTS_PER_DECADE
#   "linear" -> linear spacing using LINEAR_FREQUENCY_POINTS
FREQUENCY_SPACING_MODE = "log"
POINTS_PER_DECADE = 5             # used only when FREQUENCY_SPACING_MODE = "log"
LINEAR_FREQUENCY_POINTS = 10      # used only when FREQUENCY_SPACING_MODE = "linear"

# Main capacitance measurement sweep order.
# Options:
#   "voltage_sweep_per_frequency"
#       Current/new style: set one frequency first, then sweep all SMU voltages.
#   "frequency_sweep_per_voltage"
#       Old style: set one SMU voltage first, then sweep all frequencies.
#
# Both modes save the same detailed rows and calculate the final CV/Cj curves
# only after all available measurements are complete.
MAIN_SWEEP_ORDER = "frequency_sweep_per_voltage"

# Optional voltage pre-scan before the actual capacitance measurement.
# During this pre-scan, the FG stays at one static setting. The script only
# reads Vdc_pv and Idc_pv, so it can determine the usable voltage range before
# starting the full frequency-voltage capacitance measurement.
ENABLE_VOLTAGE_PRESCAN_FOR_TIME_ESTIMATE = True
PRESCAN_FG_FREQ_HZ = 5000.0        # [Hz]
PRESCAN_FG_VPP = 0.010             # [Vpp] 10 mVpp
PRESCAN_FG_OFFSET = 0.0            # [V]
SAVE_PRESCAN_CSV = True

# Timing estimate. This compensates for VISA/query/processing overhead that is
# not captured by the explicit sleep() calls. Increase this if the estimate is
# too optimistic on your setup.
ESTIMATE_READ_OVERHEAD_PER_POINT = 0.5       # [s] per capacitance point
ESTIMATE_SAVE_OVERHEAD_PER_OUTER_SWEEP = 0.0 # [s] per completed outer sweep

# Timing
SETTLING_TIME_AFTER_FREQ = 4     # [s]
SETTLING_TIME_AFTER_SMU = 1.0      # [s]
LOCKIN_TIME_CONSTANT_WAIT = 0.0    # [s]

# Safety
MAX_IDC_ABS = 5.0                  # [A]
STOP_IF_IDC_NEGATIVE = False
NEGATIVE_IDC_LIMIT = -1e-6         # [A]

# End-of-script output state
# The script does not turn the SMU or function generator off at the end.
# Instead, it leaves them on in the requested final state.
TURN_OFF_SMU_AT_END = False
TURN_OFF_FG_AT_END = False
SET_FINAL_OUTPUT_STATE_AT_END = True
FINAL_SMU_VOLTAGE = 12.0             # [V]
FINAL_FG_VPP = 0.010                 # [Vpp] 10 mVpp
FINAL_FG_FREQ_HZ = 5000.0            # [Hz] 5 kHz
FINAL_FG_OFFSET = 0.0                # [V]

# High-voltage capacitance clean-up
# During each fixed-frequency voltage sweep, once measured Vdc_pv is above
# HIGH_VOLTAGE_MONOTONIC_THRESHOLD_VDC, the capacitance should not drop below
# the previous kept capacitance at the same frequency. Points that violate this
# are marked in the detailed CSV and excluded from the final CV calculation.
APPLY_HIGH_VOLTAGE_MONOTONIC_FILTER = True
HIGH_VOLTAGE_MONOTONIC_THRESHOLD_VDC = 0.5   # [V], measured Vdc_pv threshold
HIGH_VOLTAGE_MONOTONIC_REL_TOL = 0.0         # 0.02 allows a 2% drop
HIGH_VOLTAGE_MONOTONIC_ABS_TOL_F = 0.0       # [F], absolute drop tolerance

# Current scaling of ADC1 on lock-in #1
ADC1_TO_AMPERE = 1.0

# Minimum usable AC current
MIN_IAC_MAG = 1e-12                # [A]

# Manual phasor inversion settings
INVERT_CURRENT_PHASOR_MANUALLY = False
INVERT_VOLTAGE_PHASOR_MANUALLY = False

# Output folder
OUTPUT_DIR = Path(".")

# Device geometry
# Active device/junction area used to calculate Cj = C / area.
# Put your measured active area here in cm^2.
DEVICE_AREA_CM2 = 126.4             # [cm^2]

# Plot settings
MAKE_CV_PLOT = True
MAKE_CJ_PLOT = True
SHOW_PLOTS = False
CAPACITANCE_UNIT = "uF"            # Options: "F", "mF", "uF", "nF"

# Y-axis scaling for the plots.
# Options: "linear" or "log"
CV_Y_AXIS_SCALE = "linear"
CJ_Y_AXIS_SCALE = "log"

# Final capacitance filtering settings per voltage point
FINAL_CAP_REQUIRE_POSITIVE = True

# Recommended for noisy measurements: 20 Hz to 3000 Hz.
# Use None to let the robust filter decide automatically.
FINAL_CAP_MIN_FREQ_HZ = None       # Example: 20.0
FINAL_CAP_MAX_FREQ_HZ = None       # Example: 3000.0

FINAL_CAP_TRIM_EDGE_POINTS = 0
FINAL_CAP_MAD_THRESHOLD = 3.5
FINAL_CAP_FILTER_ITERATIONS = 5
FINAL_CAP_MIN_POINTS = 6
FINAL_CAP_FALLBACK_REL_TOL = 0.20

PRINT_IGNORED_CAP_POINTS = False
SAVE_PROGRESS_AFTER_EACH_OUTER_SWEEP = True


# ============================================================
# VISA ADDRESSES
# ============================================================

DMM_ADDR = "GPIB0::10::INSTR"
LOCKIN_I_ADDR = "GPIB0::15::INSTR"
LOCKIN_V_ADDR = "GPIB0::12::INSTR"
FG_ADDR = "GPIB0::14::INSTR"
SMU_ADDR = "GPIB0::26::INSTR"


# ============================================================
# READ COMMANDS
# ============================================================

IAC_MAG_CMD = "MAG."
IAC_PHASE_CMD = "PHA."
IDC_ADC1_CMD = "ADC. 1"

VAC_MAG_CMD = "MAG."
VAC_PHASE_CMD = "PHA."


# ============================================================
# CONFIGURATION COMMANDS
# ============================================================

LOCKIN_I_CONFIG_COMMANDS = [
    # Add exact commands from manual if needed.
]

LOCKIN_V_CONFIG_COMMANDS = [
    # Add exact commands from manual if needed.
]


# ============================================================
# HELPER FUNCTIONS
# ============================================================

FLOAT_RE = re.compile(
    r"[-+]?(?:(?:\d+\.\d*)|(?:\.\d+)|(?:\d+))(?:[Ee][-+]?\d+)?"
)


class StopMeasurement(Exception):
    """Raised when a safety condition requests stopping the whole measurement."""


class EndVoltageSweep(Exception):
    """Raised when the current voltage sweep should stop but the measurement can continue."""


def safe_write(inst, cmd, label="instrument"):
    try:
        inst.write(cmd)
    except Exception as exc:
        print(f"WARNING: {label} rejected command {cmd!r}: {exc}")


def strict_write(inst, cmd, label="instrument"):
    try:
        inst.write(cmd)
    except Exception as exc:
        raise RuntimeError(f"{label} rejected critical command {cmd!r}: {exc}") from exc


def clean_instrument_reply(raw):
    return (
        raw.replace("\x00", "")
           .replace("\r", "")
           .replace("\n", "")
           .strip()
    )


def safe_query_float(inst, cmd, label="instrument", retries=3, delay=0.2):
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            raw = inst.query(cmd)
            cleaned = clean_instrument_reply(raw)

            try:
                value = float(cleaned)
            except ValueError:
                matches = FLOAT_RE.findall(cleaned)
                if not matches:
                    raise ValueError(
                        f"{label} returned raw={raw!r}, cleaned={cleaned!r} "
                        f"for command {cmd!r}, no float found."
                    )
                value = float(matches[-1])

            if not math.isfinite(value):
                raise ValueError(
                    f"{label} returned non-finite value {value!r} "
                    f"for command {cmd!r}."
                )

            return value

        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(delay)

    raise ValueError(
        f"Failed to query {label} with command {cmd!r} after {retries} tries. "
        f"Last error: {last_error}"
    )


def logspace_points(f_start, f_stop, points_per_decade):
    if f_start <= 0 or f_stop <= 0:
        raise ValueError("F_START and F_STOP must be positive for logarithmic spacing.")
    if f_stop <= f_start:
        raise ValueError("F_STOP must be larger than F_START.")
    if points_per_decade <= 0:
        raise ValueError("POINTS_PER_DECADE must be positive.")

    decades = math.log10(f_stop) - math.log10(f_start)
    n_points = int(math.ceil(decades * points_per_decade)) + 1

    freqs = []
    for k in range(n_points):
        exponent = math.log10(f_start) + k * decades / (n_points - 1)
        freqs.append(10 ** exponent)

    return freqs


def linspace_points(start, stop, number_of_points):
    if number_of_points < 2:
        raise ValueError("LINEAR_FREQUENCY_POINTS must be at least 2.")
    if stop <= start:
        raise ValueError("F_STOP must be larger than F_START.")

    step = (stop - start) / (number_of_points - 1)
    return [start + k * step for k in range(number_of_points)]


def frequency_points(f_start, f_stop, spacing_mode, points_per_decade, linear_frequency_points):
    mode = spacing_mode.lower().strip()

    if mode == "log":
        return logspace_points(f_start, f_stop, points_per_decade)
    if mode == "linear":
        return linspace_points(f_start, f_stop, linear_frequency_points)

    raise ValueError('FREQUENCY_SPACING_MODE must be either "log" or "linear".')


def linear_points(start, stop, step):
    if step <= 0:
        raise ValueError("SMU_SWEEP_STEP_VOLTAGE must be positive.")
    if stop < start:
        raise ValueError("SMU_SWEEP_STOP_VOLTAGE must be >= SMU_SWEEP_START_VOLTAGE.")

    points = []
    value = start
    while value <= stop + 1e-12:
        points.append(round(value, 12))
        value += step
    return points


def wrap_phase_deg(phi):
    return ((phi + 180.0) % 360.0) - 180.0


def normalize_signed_phasor(magnitude, phase_deg):
    if magnitude < 0:
        magnitude = -magnitude
        phase_deg = wrap_phase_deg(phase_deg + 180.0)

    phase_deg = wrap_phase_deg(phase_deg)
    return magnitude, phase_deg


def invert_phasor(magnitude, phase_deg):
    magnitude = abs(magnitude)
    phase_deg = wrap_phase_deg(phase_deg + 180.0)
    return magnitude, phase_deg


def impedance_from_mag_phase(vac_mag, vac_phase_deg, iac_mag, iac_phase_deg):
    if iac_mag <= 0:
        raise ValueError("Iac magnitude must be positive.")

    z_mag = vac_mag / iac_mag
    z_phase_deg = wrap_phase_deg(vac_phase_deg - iac_phase_deg)
    z_phase_rad = math.radians(z_phase_deg)

    z_real = z_mag * math.cos(z_phase_rad)
    z_imag = z_mag * math.sin(z_phase_rad)

    return z_mag, z_phase_deg, z_real, z_imag


def capacitance_from_impedance(z_real, z_imag, frequency_hz):
    omega = 2.0 * math.pi * frequency_hz
    z_complex = complex(z_real, z_imag)

    if abs(z_complex) <= 0 or omega <= 0:
        return float("nan"), float("nan"), float("nan")

    y_complex = 1.0 / z_complex
    c_uncorrected = y_complex.imag / omega

    return c_uncorrected, y_complex.real, y_complex.imag


def capacitance_scale_factor(unit):
    unit = unit.lower()

    if unit == "f":
        return 1.0, "F"
    if unit == "mf":
        return 1e3, "mF"
    if unit == "uf":
        return 1e6, "uF"
    if unit == "nf":
        return 1e9, "nF"

    raise ValueError("Capacitance unit must be one of: 'F', 'mF', 'uF', 'nF'")


def format_capacitance_value(value_farad, unit):
    scale, unit_label = capacitance_scale_factor(unit)
    return f"{value_farad * scale:.6g} {unit_label}"


def capacitance_density_uf_per_cm2(value_farad, area_cm2=DEVICE_AREA_CM2):
    """Convert capacitance in farad to Cj in uF/cm^2."""

    if area_cm2 <= 0:
        raise ValueError("DEVICE_AREA_CM2 must be positive to calculate Cj.")

    return value_farad * 1e6 / area_cm2


def format_capacitance_density_value(value_farad, area_cm2=DEVICE_AREA_CM2):
    if value_farad is None or not math.isfinite(value_farad):
        return "nan uF/cm^2"

    return f"{capacitance_density_uf_per_cm2(value_farad, area_cm2):.6g} uF/cm^2"


def save_csv(rows, csv_file):
    if not rows:
        print(f"\nNo data rows recorded for {csv_file}.")
        return

    # Use the union of all keys so later-added diagnostic fields, such as
    # high-voltage discard flags, are always written safely.
    fieldnames = []
    seen = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)

    with csv_file.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Saved {len(rows)} rows to: {csv_file}")


def check_dc_safety(vdc_pv, idc_pv, negative_current_ends_voltage_sweep=False):
    """Check DC safety limits.

    By default, every limit stops the full measurement. During the normal
    voltage sweep, negative Idc_pv can be treated as the end of that voltage
    sweep only, so the script can continue with the next frequency.
    """

    # A large current magnitude is still a full safety stop, even if the
    # current is negative. This keeps the compliance/safety behavior strict.
    if abs(idc_pv) > MAX_IDC_ABS:
        raise StopMeasurement(
            f"abs(Idc_pv) exceeded limit: {abs(idc_pv):.6e} A > {MAX_IDC_ABS:.6e} A"
        )

    if STOP_IF_IDC_NEGATIVE and idc_pv < NEGATIVE_IDC_LIMIT:
        message = (
            f"Idc_pv became negative: {idc_pv:.6e} A < "
            f"{NEGATIVE_IDC_LIMIT:.6e} A"
        )
        if negative_current_ends_voltage_sweep:
            raise EndVoltageSweep(message)
        raise StopMeasurement(message)

    if STOP_IF_VDC_EXCEEDS_MAX and vdc_pv > MAX_VDC_PV:
        raise StopMeasurement(
            f"Vdc_pv exceeded limit: {vdc_pv:.6e} V > {MAX_VDC_PV:.6e} V"
        )


def set_smu_voltage_and_read_dc(smu, dmm, lockin_i, smu_voltage):
    strict_write(smu, f"smua.source.levelv = {smu_voltage}", "SMU")
    time.sleep(SETTLING_TIME_AFTER_SMU)

    vdc_pv = safe_query_float(dmm, "READ?", "DMM")
    idc_adc1 = safe_query_float(lockin_i, IDC_ADC1_CMD, "Lock-in current ADC1")
    idc_pv = idc_adc1 * ADC1_TO_AMPERE

    return vdc_pv, idc_pv


def find_first_positive_vdc_point(smu, dmm, lockin_i, smu_points):
    print("\nFinding first positive Vdc_pv point...")

    for smu_voltage in smu_points:
        vdc_pv, idc_pv = set_smu_voltage_and_read_dc(smu, dmm, lockin_i, smu_voltage)

        print(
            f"  SMU={smu_voltage:.6g} V | "
            f"Vdc_pv={vdc_pv:.6e} V | "
            f"Idc_pv={idc_pv:.6e} A"
        )

        check_dc_safety(vdc_pv, idc_pv)

        if vdc_pv >= VDC_POSITIVE_THRESHOLD:
            print(
                f"First positive point found: SMU={smu_voltage:.6g} V, "
                f"Vdc_pv={vdc_pv:.6e} V"
            )
            return smu_voltage

    raise RuntimeError(
        "Could not find a positive Vdc_pv point within the SMU sweep range."
    )



def format_duration(seconds):
    """Return a readable duration string for terminal output."""

    if seconds is None or not math.isfinite(seconds):
        return "unknown"

    seconds = max(0, int(round(seconds)))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    if hours > 0:
        return f"{hours:d} h {minutes:d} min {secs:d} s"
    if minutes > 0:
        return f"{minutes:d} min {secs:d} s"
    return f"{secs:d} s"


def estimate_capacitance_measurement_time_seconds(
    n_frequencies,
    n_voltage_points,
    sweep_order,
):
    """Estimate duration of the actual capacitance measurement phase."""

    if n_frequencies <= 0 or n_voltage_points <= 0:
        return 0.0

    frequency_wait = SETTLING_TIME_AFTER_FREQ + LOCKIN_TIME_CONSTANT_WAIT
    read_overhead = ESTIMATE_READ_OVERHEAD_PER_POINT
    save_overhead = ESTIMATE_SAVE_OVERHEAD_PER_OUTER_SWEEP
    total_points = n_frequencies * n_voltage_points

    if sweep_order == "voltage_sweep_per_frequency":
        return (
            n_frequencies * (frequency_wait + save_overhead)
            + total_points * (SETTLING_TIME_AFTER_SMU + read_overhead)
        )

    if sweep_order == "frequency_sweep_per_voltage":
        return (
            n_voltage_points * (SETTLING_TIME_AFTER_SMU + save_overhead)
            + total_points * (frequency_wait + read_overhead)
        )

    raise ValueError("Unknown sweep order for time estimate.")


def print_capacitance_time_estimate(n_frequencies, n_voltage_points, sweep_order):
    estimated_seconds = estimate_capacitance_measurement_time_seconds(
        n_frequencies=n_frequencies,
        n_voltage_points=n_voltage_points,
        sweep_order=sweep_order,
    )
    total_tests = n_frequencies * n_voltage_points

    print("\nEstimated capacitance measurement time:")
    print(f"  Sweep order:              {sweep_order}")
    print(f"  Frequencies:              {n_frequencies}")
    print(f"  Voltage points:           {n_voltage_points}")
    print(f"  Total capacitance points: {total_tests}")
    print(f"  Estimated duration:       {format_duration(estimated_seconds)}")
    if sweep_order == "voltage_sweep_per_frequency":
        print(
            "  Estimate uses one frequency settling wait per frequency and one "
            "SMU settling wait per capacitance point."
        )
    else:
        print(
            "  Estimate uses one SMU settling wait per voltage and one frequency "
            "settling wait per capacitance point."
        )
    print(
        "  Timing settings: "
        f"frequency wait {SETTLING_TIME_AFTER_FREQ:g} s + "
        f"lock-in wait {LOCKIN_TIME_CONSTANT_WAIT:g} s, "
        f"SMU wait {SETTLING_TIME_AFTER_SMU:g} s, "
        f"read overhead {ESTIMATE_READ_OVERHEAD_PER_POINT:g} s, "
        f"save overhead per outer sweep {ESTIMATE_SAVE_OVERHEAD_PER_OUTER_SWEEP:g} s."
    )


def voltage_prescan_for_positive_current(
    smu,
    dmm,
    lockin_i,
    fg,
    smu_points_all,
    prescan_csv_file=None,
):
    """Pre-scan the voltage range with static FG settings.

    The pre-scan does not read AC magnitude/phase and does not calculate
    capacitance. It only finds the first positive measured Vdc_pv point and
    the last voltage point before Idc_pv becomes negative.
    """

    print("\nPre-scan enabled: finding usable voltage range before capacitance measurements...")
    print(
        f"  Static FG during pre-scan: {PRESCAN_FG_VPP:g} Vpp, "
        f"{PRESCAN_FG_FREQ_HZ:g} Hz, offset {PRESCAN_FG_OFFSET:g} V"
    )

    strict_write(fg, f"FUNC {FG_WAVEFORM}", "Function generator")
    strict_write(fg, f"VOLT {PRESCAN_FG_VPP}", "Function generator")
    strict_write(fg, f"VOLT:OFFS {PRESCAN_FG_OFFSET}", "Function generator")
    strict_write(fg, f"FREQ {PRESCAN_FG_FREQ_HZ}", "Function generator")
    strict_write(fg, "OUTP ON", "Function generator")
    time.sleep(SETTLING_TIME_AFTER_FREQ + LOCKIN_TIME_CONSTANT_WAIT)

    prescan_rows = []
    first_positive_smu = None
    first_positive_vdc = None
    first_positive_index = None
    last_current_positive_smu = None
    last_current_positive_vdc = None
    last_current_positive_idc = None
    negative_stop_smu = None
    negative_stop_vdc = None
    negative_stop_idc = None

    for voltage_index, smu_voltage in enumerate(smu_points_all, start=1):
        vdc_pv, idc_pv = set_smu_voltage_and_read_dc(
            smu=smu,
            dmm=dmm,
            lockin_i=lockin_i,
            smu_voltage=smu_voltage,
        )

        row = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "prescan_index": voltage_index,
            "smu_voltage_V": smu_voltage,
            "Vdc_pv_V": vdc_pv,
            "Idc_pv_A": idc_pv,
            "fg_frequency_Hz": PRESCAN_FG_FREQ_HZ,
            "fg_vpp_V": PRESCAN_FG_VPP,
            "current_negative_stop": False,
        }
        prescan_rows.append(row)

        print(
            f"  pre-scan {voltage_index:>3}/{len(smu_points_all)} | "
            f"SMU={smu_voltage:.6g} V | "
            f"Vdc_pv={vdc_pv:.6e} V | "
            f"Idc_pv={idc_pv:.6e} A"
        )

        try:
            check_dc_safety(
                vdc_pv=vdc_pv,
                idc_pv=idc_pv,
                negative_current_ends_voltage_sweep=True,
            )
        except EndVoltageSweep:
            row["current_negative_stop"] = True
            negative_stop_smu = smu_voltage
            negative_stop_vdc = vdc_pv
            negative_stop_idc = idc_pv
            print(
                "  Pre-scan stop: Idc_pv became negative. "
                "This is treated as the end of the usable voltage range."
            )
            break

        if first_positive_smu is None and vdc_pv >= VDC_POSITIVE_THRESHOLD:
            first_positive_smu = smu_voltage
            first_positive_vdc = vdc_pv
            first_positive_index = voltage_index

        if first_positive_smu is not None:
            last_current_positive_smu = smu_voltage
            last_current_positive_vdc = vdc_pv
            last_current_positive_idc = idc_pv

    if SAVE_PRESCAN_CSV and prescan_csv_file is not None:
        save_csv(prescan_rows, prescan_csv_file)

    if first_positive_smu is None:
        raise RuntimeError(
            "Pre-scan could not find a positive Vdc_pv point within the SMU sweep range."
        )

    if last_current_positive_smu is None:
        raise RuntimeError(
            "Pre-scan found positive Vdc_pv but no usable positive-current voltage points."
        )

    usable_smu_points = [
        v for v in smu_points_all
        if first_positive_smu - 1e-12 <= v <= last_current_positive_smu + 1e-12
    ]

    print("\nPre-scan result:")
    print(
        f"  First positive Vdc_pv: SMU={first_positive_smu:.6g} V, "
        f"Vdc_pv={first_positive_vdc:.6e} V, index={first_positive_index}"
    )
    print(
        f"  Last positive-current point: SMU={last_current_positive_smu:.6g} V, "
        f"Vdc_pv={last_current_positive_vdc:.6e} V, "
        f"Idc_pv={last_current_positive_idc:.6e} A"
    )
    if negative_stop_smu is not None:
        print(
            f"  First negative-current point: SMU={negative_stop_smu:.6g} V, "
            f"Vdc_pv={negative_stop_vdc:.6e} V, "
            f"Idc_pv={negative_stop_idc:.6e} A"
        )
    else:
        print("  No negative-current point found within the configured sweep range.")
    print(f"  Usable voltage points for capacitance measurement: {len(usable_smu_points)}")

    return {
        "prescan_rows": prescan_rows,
        "first_positive_smu_voltage": first_positive_smu,
        "first_positive_vdc_pv": first_positive_vdc,
        "last_current_positive_smu_voltage": last_current_positive_smu,
        "last_current_positive_vdc_pv": last_current_positive_vdc,
        "last_current_positive_idc_pv": last_current_positive_idc,
        "negative_stop_smu_voltage": negative_stop_smu,
        "negative_stop_vdc_pv": negative_stop_vdc,
        "negative_stop_idc_pv": negative_stop_idc,
        "usable_smu_points": usable_smu_points,
    }

def read_ac_phasors(lockin_i, lockin_v):
    iac_mag_raw = safe_query_float(lockin_i, IAC_MAG_CMD, "Lock-in current magnitude")
    iac_phase_raw = safe_query_float(lockin_i, IAC_PHASE_CMD, "Lock-in current phase")

    iac_mag, iac_phase = normalize_signed_phasor(iac_mag_raw, iac_phase_raw)

    if INVERT_CURRENT_PHASOR_MANUALLY:
        iac_mag, iac_phase = invert_phasor(iac_mag, iac_phase)

    vac_mag_raw = safe_query_float(lockin_v, VAC_MAG_CMD, "Lock-in voltage magnitude")
    vac_phase_raw = safe_query_float(lockin_v, VAC_PHASE_CMD, "Lock-in voltage phase")

    vac_mag, vac_phase = normalize_signed_phasor(vac_mag_raw, vac_phase_raw)

    if INVERT_VOLTAGE_PHASOR_MANUALLY:
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


def measure_point_at_frequency_and_voltage(
    test_index,
    frequency_index,
    voltage_index,
    f_ac,
    smu_voltage,
    smu,
    dmm,
    lockin_i,
    lockin_v,
    set_smu_before_measurement=True,
):
    """Measure one matrix point: fixed frequency and fixed SMU voltage.

    When set_smu_before_measurement is True, the function sets the SMU and
    waits SETTLING_TIME_AFTER_SMU before reading DC values. This is used in
    voltage-sweep-per-frequency mode.

    When set_smu_before_measurement is False, the SMU is assumed to already be
    at smu_voltage. The function only re-reads Vdc_pv and Idc_pv, then reads
    the AC phasors. This avoids waiting for the same SMU voltage before every
    frequency point in frequency-sweep-per-voltage mode.
    """

    if set_smu_before_measurement:
        vdc_pv, idc_pv = set_smu_voltage_and_read_dc(
            smu=smu,
            dmm=dmm,
            lockin_i=lockin_i,
            smu_voltage=smu_voltage,
        )
    else:
        vdc_pv = safe_query_float(dmm, "READ?", "DMM")
        idc_adc1 = safe_query_float(lockin_i, IDC_ADC1_CMD, "Lock-in current ADC1")
        idc_pv = idc_adc1 * ADC1_TO_AMPERE

    check_dc_safety(
        vdc_pv,
        idc_pv,
        negative_current_ends_voltage_sweep=True,
    )

    ph = read_ac_phasors(lockin_i, lockin_v)

    iac_mag = ph["Iac_mag_corrected_A"]
    iac_phase = ph["Iac_phase_corrected_deg"]
    vac_mag = ph["Vac_mag_corrected_V"]
    vac_phase = ph["Vac_phase_corrected_deg"]

    if iac_mag <= MIN_IAC_MAG:
        print(
            f"    V point {voltage_index:>3} | "
            f"SMU={smu_voltage:.6g} V | "
            f"Vdc={vdc_pv:.6e} V | WARNING: Iac too small, skipping"
        )
        return None

    z_mag, z_phase, z_real, z_imag = impedance_from_mag_phase(
        vac_mag=vac_mag,
        vac_phase_deg=vac_phase,
        iac_mag=iac_mag,
        iac_phase_deg=iac_phase,
    )

    c_uncorrected, y_real, y_imag = capacitance_from_impedance(
        z_real=z_real,
        z_imag=z_imag,
        frequency_hz=f_ac,
    )

    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "test_index": test_index,
        "frequency_index": frequency_index,
        "voltage_index": voltage_index,
        "smu_voltage_V": smu_voltage,
        "Vdc_pv_V": vdc_pv,
        "Idc_pv_A": idc_pv,
        "f_ac_Hz": f_ac,

        "Vac_mag_raw_V": ph["Vac_mag_raw_V"],
        "Vac_phase_raw_deg": ph["Vac_phase_raw_deg"],
        "Vac_mag_corrected_V": vac_mag,
        "Vac_phase_corrected_deg": vac_phase,

        "Iac_mag_raw_A": ph["Iac_mag_raw_A"],
        "Iac_phase_raw_deg": ph["Iac_phase_raw_deg"],
        "Iac_mag_corrected_A": iac_mag,
        "Iac_phase_corrected_deg": iac_phase,

        "Z_mag_ohm": z_mag,
        "Z_phase_deg": z_phase,
        "Z_real_ohm": z_real,
        "Z_imag_ohm": z_imag,

        "Y_real_S": y_real,
        "Y_imag_S": y_imag,
        "C_uncorrected_F": c_uncorrected,

        # Filled/updated by apply_high_voltage_monotonic_filter() after all
        # frequency-voltage measurements are complete.
        "high_v_monotonic_discarded": False,
        "high_v_monotonic_reason": "",
        "high_v_previous_kept_C_F": float("nan"),
        "high_v_previous_kept_Vdc_pv_V": float("nan"),
        "high_v_allowed_min_C_F": float("nan"),
    }

    print(
        f"    V point {voltage_index:>3} | "
        f"SMU={smu_voltage:.6g} V | "
        f"Vdc={vdc_pv:.6e} V | "
        f"C={format_capacitance_value(c_uncorrected, CAPACITANCE_UNIT)}"
    )

    return row


def voltage_sweep_at_frequency(
    frequency_index,
    f_ac,
    smu,
    dmm,
    lockin_i,
    lockin_v,
    fg,
    smu_points,
    total_frequencies,
):
    """At one fixed frequency, sweep all voltage points."""

    rows = []

    print(
        f"\nFrequency point {frequency_index}/{total_frequencies}: "
        f"f={f_ac:.6g} Hz"
    )

    strict_write(fg, f"FREQ {f_ac}", "Function generator")
    time.sleep(SETTLING_TIME_AFTER_FREQ + LOCKIN_TIME_CONSTANT_WAIT)

    for voltage_index, smu_voltage in enumerate(smu_points, start=1):
        test_index = (frequency_index - 1) * len(smu_points) + voltage_index

        try:
            row = measure_point_at_frequency_and_voltage(
                test_index=test_index,
                frequency_index=frequency_index,
                voltage_index=voltage_index,
                f_ac=f_ac,
                smu_voltage=smu_voltage,
                smu=smu,
                dmm=dmm,
                lockin_i=lockin_i,
                lockin_v=lockin_v,
            )
        except EndVoltageSweep as exc:
            print(
                f"    Ending voltage sweep at this frequency: {exc}. "
                f"Moving to the next frequency."
            )
            break

        if row is not None:
            rows.append(row)

    return rows


def frequency_sweep_at_voltage(
    voltage_index,
    smu_voltage,
    smu,
    dmm,
    lockin_i,
    lockin_v,
    fg,
    freqs,
    total_voltages,
):
    """At one fixed SMU voltage, sweep all frequency points."""

    rows = []

    print(
        f"\nVoltage point {voltage_index}/{total_voltages}: "
        f"SMU={smu_voltage:.6g} V"
    )

    try:
        vdc_start, idc_start = set_smu_voltage_and_read_dc(
            smu=smu,
            dmm=dmm,
            lockin_i=lockin_i,
            smu_voltage=smu_voltage,
        )
        check_dc_safety(
            vdc_start,
            idc_start,
            negative_current_ends_voltage_sweep=True,
        )
    except EndVoltageSweep as exc:
        print(
            f"  Ending remaining voltage points before frequency sweep: {exc}."
        )
        return rows, True

    print(
        f"  Vdc_pv_start={vdc_start:.6e} V | "
        f"Idc_pv_start={idc_start:.6e} A"
    )

    for frequency_index, f_ac in enumerate(freqs, start=1):
        test_index = (voltage_index - 1) * len(freqs) + frequency_index

        strict_write(fg, f"FREQ {f_ac}", "Function generator")
        time.sleep(SETTLING_TIME_AFTER_FREQ + LOCKIN_TIME_CONSTANT_WAIT)

        try:
            row = measure_point_at_frequency_and_voltage(
                test_index=test_index,
                frequency_index=frequency_index,
                voltage_index=voltage_index,
                f_ac=f_ac,
                smu_voltage=smu_voltage,
                smu=smu,
                dmm=dmm,
                lockin_i=lockin_i,
                lockin_v=lockin_v,
                set_smu_before_measurement=False,
            )
        except EndVoltageSweep as exc:
            print(
                f"    Ending frequency sweep at this voltage: {exc}. "
                "Remaining frequencies at this voltage are skipped."
            )
            return rows, True

        if row is not None:
            rows.append(row)

    return rows, False


def capacitance_value_is_usable_for_monotonic_check(value):
    return (
        value is not None
        and math.isfinite(value)
        and (not FINAL_CAP_REQUIRE_POSITIVE or value > 0)
    )


def apply_high_voltage_monotonic_filter(rows):
    """
    Mark capacitance points that violate the high-voltage monotonic rule.

    The check is done independently for each fixed frequency. While sweeping
    upward in voltage, any point with measured Vdc_pv above the configured
    threshold is rejected if its capacitance is lower than the previous kept
    capacitance at that same frequency. Rejected rows stay in the detailed CSV
    for traceability, but filter_capacitance_rows() excludes them from the final
    CV calculation.
    """

    for row in rows:
        row["high_v_monotonic_discarded"] = False
        row["high_v_monotonic_reason"] = ""
        row["high_v_previous_kept_C_F"] = float("nan")
        row["high_v_previous_kept_Vdc_pv_V"] = float("nan")
        row["high_v_allowed_min_C_F"] = float("nan")

    if not APPLY_HIGH_VOLTAGE_MONOTONIC_FILTER:
        return rows

    grouped_by_frequency = {}
    for row in rows:
        key = row.get("frequency_index", row.get("f_ac_Hz"))
        grouped_by_frequency.setdefault(key, []).append(row)

    discarded_count = 0

    for frequency_key, frequency_rows in grouped_by_frequency.items():
        frequency_rows.sort(
            key=lambda r: (
                r.get("voltage_index", 10**12),
                r.get("smu_voltage_V", float("inf")),
            )
        )

        previous_kept_c = None
        previous_kept_vdc = None

        for row in frequency_rows:
            c = row.get("C_uncorrected_F")
            vdc = row.get("Vdc_pv_V")

            if not capacitance_value_is_usable_for_monotonic_check(c):
                continue
            if vdc is None or not math.isfinite(vdc):
                continue

            if previous_kept_c is not None and vdc > HIGH_VOLTAGE_MONOTONIC_THRESHOLD_VDC:
                allowed_min_c = (
                    previous_kept_c * (1.0 - HIGH_VOLTAGE_MONOTONIC_REL_TOL)
                    - HIGH_VOLTAGE_MONOTONIC_ABS_TOL_F
                )

                if c < allowed_min_c:
                    row["high_v_monotonic_discarded"] = True
                    row["high_v_monotonic_reason"] = (
                        "Vdc_pv above threshold and C lower than previous kept C "
                        "at the same frequency"
                    )
                    row["high_v_previous_kept_C_F"] = previous_kept_c
                    row["high_v_previous_kept_Vdc_pv_V"] = previous_kept_vdc
                    row["high_v_allowed_min_C_F"] = allowed_min_c
                    discarded_count += 1
                    continue

            previous_kept_c = c
            previous_kept_vdc = vdc

    print(
        f"\nHigh-voltage monotonic capacitance filter: discarded "
        f"{discarded_count} point(s) where Vdc_pv > "
        f"{HIGH_VOLTAGE_MONOTONIC_THRESHOLD_VDC:g} V and C dropped below "
        f"the previous kept C at the same frequency."
    )

    return rows


def filter_capacitance_rows(rows):
    if not rows:
        return None

    candidate_points = []

    for idx, row in enumerate(rows, start=1):
        f = row.get("f_ac_Hz")
        c = row.get("C_uncorrected_F")

        if row.get("high_v_monotonic_discarded", False):
            continue
        if f is None or c is None:
            continue
        if not (math.isfinite(f) and math.isfinite(c)):
            continue
        if f <= 0:
            continue
        if FINAL_CAP_REQUIRE_POSITIVE and c <= 0:
            continue
        if FINAL_CAP_MIN_FREQ_HZ is not None and f < FINAL_CAP_MIN_FREQ_HZ:
            continue
        if FINAL_CAP_MAX_FREQ_HZ is not None and f > FINAL_CAP_MAX_FREQ_HZ:
            continue

        candidate_points.append({
            "point_index": idx,
            "frequency_hz": f,
            "capacitance_f": c,
        })

    candidate_points.sort(key=lambda p: p["frequency_hz"])

    if FINAL_CAP_TRIM_EDGE_POINTS > 0:
        n_trim = FINAL_CAP_TRIM_EDGE_POINTS
        if len(candidate_points) > 2 * n_trim:
            candidate_points = candidate_points[n_trim:-n_trim]

    if len(candidate_points) < FINAL_CAP_MIN_POINTS:
        return {
            "ok": False,
            "reason": "not enough valid candidate points",
            "candidate_count": len(candidate_points),
        }

    kept = candidate_points[:]
    outlier_rejected = []
    final_baseline = None
    final_tolerance = None

    for iteration in range(1, FINAL_CAP_FILTER_ITERATIONS + 1):
        cap_values = [p["capacitance_f"] for p in kept]
        baseline = statistics.median(cap_values)
        deviations = [abs(c - baseline) for c in cap_values]
        mad = statistics.median(deviations)

        if mad <= 0:
            tolerance = abs(baseline) * FINAL_CAP_FALLBACK_REL_TOL
            if tolerance <= 0:
                tolerance = 1e-30
        else:
            tolerance = FINAL_CAP_MAD_THRESHOLD * 1.4826 * mad

        new_kept = []
        new_outliers = []

        for p in kept:
            deviation = abs(p["capacitance_f"] - baseline)
            if deviation <= tolerance:
                new_kept.append(p)
            else:
                rejected_point = dict(p)
                rejected_point["deviation_f"] = deviation
                rejected_point["baseline_f"] = baseline
                rejected_point["tolerance_f"] = tolerance
                rejected_point["iteration"] = iteration
                new_outliers.append(rejected_point)

        if len(new_kept) < FINAL_CAP_MIN_POINTS:
            final_baseline = baseline
            final_tolerance = tolerance
            break

        final_baseline = baseline
        final_tolerance = tolerance

        if not new_outliers:
            kept = new_kept
            break

        outlier_rejected.extend(new_outliers)
        kept = new_kept

    final_values = [p["capacitance_f"] for p in kept]
    final_median = statistics.median(final_values)
    final_mean = sum(final_values) / len(final_values)
    final_std = statistics.stdev(final_values) if len(final_values) > 1 else 0.0
    used_freqs = [p["frequency_hz"] for p in kept]

    return {
        "ok": True,
        "final_median_F": final_median,
        "final_mean_F": final_mean,
        "final_std_F": final_std,
        "used_points": kept,
        "used_count": len(kept),
        "candidate_count": len(candidate_points),
        "outlier_rejected": outlier_rejected,
        "outlier_rejected_count": len(outlier_rejected),
        "frequency_min_Hz": min(used_freqs),
        "frequency_max_Hz": max(used_freqs),
        "last_baseline_F": final_baseline,
        "last_tolerance_F": final_tolerance,
    }


def print_filter_result(result):
    if result is None:
        print("  Final C: no rows available")
        return

    if not result.get("ok"):
        print(
            f"  Final C: FAILED, {result.get('reason')} | "
            f"valid candidates={result.get('candidate_count')}"
        )
        return

    print(
        f"  Final C median = {format_capacitance_value(result['final_median_F'], CAPACITANCE_UNIT)} | "
        f"mean +/- std = {format_capacitance_value(result['final_mean_F'], CAPACITANCE_UNIT)} +/- "
        f"{format_capacitance_value(result['final_std_F'], CAPACITANCE_UNIT)} | "
        f"used {result['used_count']}/{result['candidate_count']} points | "
        f"f used {result['frequency_min_Hz']:.6g} Hz to {result['frequency_max_Hz']:.6g} Hz"
    )


def create_cv_summary_row(sweep_index, smu_voltage, sweep_rows, filter_result):
    vdc_values = [r["Vdc_pv_V"] for r in sweep_rows if math.isfinite(r["Vdc_pv_V"])]
    idc_values = [r["Idc_pv_A"] for r in sweep_rows if math.isfinite(r["Idc_pv_A"])]

    vdc_median = statistics.median(vdc_values) if vdc_values else float("nan")
    vdc_mean = sum(vdc_values) / len(vdc_values) if vdc_values else float("nan")
    idc_median = statistics.median(idc_values) if idc_values else float("nan")

    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "sweep_index": sweep_index,
        "smu_voltage_V": smu_voltage,
        "Vdc_pv_median_V": vdc_median,
        "Vdc_pv_mean_V": vdc_mean,
        "Idc_pv_median_A": idc_median,
        "frequency_points_recorded": len(sweep_rows),
        "high_v_monotonic_discarded_points": sum(
            1 for r in sweep_rows if r.get("high_v_monotonic_discarded", False)
        ),
    }

    if filter_result and filter_result.get("ok"):
        c_final_median = filter_result["final_median_F"]
        c_final_mean = filter_result["final_mean_F"]
        c_final_std = filter_result["final_std_F"]

        row.update({
            "C_final_median_F": c_final_median,
            "C_final_mean_F": c_final_mean,
            "C_final_std_F": c_final_std,
            "Cj_final_median_uF_per_cm2": capacitance_density_uf_per_cm2(c_final_median),
            "Cj_final_mean_uF_per_cm2": capacitance_density_uf_per_cm2(c_final_mean),
            "Cj_final_std_uF_per_cm2": capacitance_density_uf_per_cm2(c_final_std),
            "device_area_cm2": DEVICE_AREA_CM2,
            "filter_used_points": filter_result["used_count"],
            "filter_candidate_points": filter_result["candidate_count"],
            "filter_outlier_points": filter_result["outlier_rejected_count"],
            "filter_frequency_min_Hz": filter_result["frequency_min_Hz"],
            "filter_frequency_max_Hz": filter_result["frequency_max_Hz"],
        })
    else:
        row.update({
            "C_final_median_F": float("nan"),
            "C_final_mean_F": float("nan"),
            "C_final_std_F": float("nan"),
            "Cj_final_median_uF_per_cm2": float("nan"),
            "Cj_final_mean_uF_per_cm2": float("nan"),
            "Cj_final_std_uF_per_cm2": float("nan"),
            "device_area_cm2": DEVICE_AREA_CM2,
            "filter_used_points": 0,
            "filter_candidate_points": 0,
            "filter_outlier_points": 0,
            "filter_frequency_min_Hz": float("nan"),
            "filter_frequency_max_Hz": float("nan"),
        })

    return row


def calculate_cv_rows_from_all_measurements(all_rows, smu_points):
    """Group all measured matrix data by voltage point and calculate final CV rows."""

    cv_rows = []

    print("\nCalculating CV curve after completing all frequency-voltage tests...")

    for voltage_index, smu_voltage in enumerate(smu_points, start=1):
        voltage_rows = [
            row for row in all_rows
            if abs(row.get("smu_voltage_V", float("nan")) - smu_voltage) <= 1e-9
        ]

        print(
            f"\nVoltage point {voltage_index}: "
            f"SMU={smu_voltage:.6g} V | "
            f"frequency rows recorded={len(voltage_rows)}"
        )

        filter_result = filter_capacitance_rows(voltage_rows)
        print_filter_result(filter_result)

        cv_row = create_cv_summary_row(
            sweep_index=voltage_index,
            smu_voltage=smu_voltage,
            sweep_rows=voltage_rows,
            filter_result=filter_result,
        )

        cv_row["voltage_index"] = voltage_index
        cv_rows.append(cv_row)

        print(
            f"  CV point calculated: Vdc_pv={cv_row['Vdc_pv_median_V']:.6e} V | "
            f"C={format_capacitance_value(cv_row['C_final_median_F'], CAPACITANCE_UNIT)} | "
            f"Cj={cv_row['Cj_final_median_uF_per_cm2']:.6g} uF/cm^2"
        )

    return cv_rows


def apply_y_axis_scale(axis_scale, quantity_label):
    mode = axis_scale.lower().strip()

    if mode == "linear":
        return
    if mode == "log":
        plt.yscale("log")
        return

    raise ValueError(f"{quantity_label} y-axis scale must be either 'linear' or 'log'.")



def plot_cv_curve(cv_rows, plot_file):
    valid_rows = []

    for row in cv_rows:
        v = row.get("Vdc_pv_median_V")
        c = row.get("C_final_median_F")
        if v is not None and c is not None and math.isfinite(v) and math.isfinite(c):
            if CV_Y_AXIS_SCALE.lower().strip() == "log" and c <= 0:
                continue
            valid_rows.append(row)

    if not valid_rows:
        print("No valid CV data available for plotting.")
        return

    valid_rows.sort(key=lambda r: r["Vdc_pv_median_V"])

    scale, unit_label = capacitance_scale_factor(CAPACITANCE_UNIT)
    voltages = [r["Vdc_pv_median_V"] for r in valid_rows]
    capacitances = [r["C_final_median_F"] * scale for r in valid_rows]

    plt.figure(figsize=(8, 5))
    plt.plot(voltages, capacitances, marker="o", linestyle="-")
    apply_y_axis_scale(CV_Y_AXIS_SCALE, "CV")
    plt.xlabel("Vdc_pv [V]")
    plt.ylabel(f"Filtered capacitance [{unit_label}]")
    plt.title(f"CV curve, y-axis = {CV_Y_AXIS_SCALE.lower().strip()}")
    plt.grid(True, which="both")
    plt.tight_layout()
    plt.savefig(plot_file, dpi=300)
    print(f"Saved CV plot to: {plot_file}")

    if SHOW_PLOTS:
        plt.show()

    plt.close()


def plot_cj_curve(cv_rows, plot_file):
    valid_rows = []

    for row in cv_rows:
        v = row.get("Vdc_pv_median_V")
        cj = row.get("Cj_final_median_uF_per_cm2")
        if v is not None and cj is not None and math.isfinite(v) and math.isfinite(cj):
            if CJ_Y_AXIS_SCALE.lower().strip() == "log" and cj <= 0:
                continue
            valid_rows.append(row)

    if not valid_rows:
        print("No valid Cj data available for plotting.")
        return

    valid_rows.sort(key=lambda r: r["Vdc_pv_median_V"])

    voltages = [r["Vdc_pv_median_V"] for r in valid_rows]
    capacitance_densities = [r["Cj_final_median_uF_per_cm2"] for r in valid_rows]

    plt.figure(figsize=(8, 5))
    plt.plot(voltages, capacitance_densities, marker="o", linestyle="-")
    apply_y_axis_scale(CJ_Y_AXIS_SCALE, "Cj")
    plt.xlabel("Vdc_pv [V]")
    plt.ylabel("Junction capacitance density Cj [uF/cm^2]")
    plt.title(
        f"Cj curve, area = {DEVICE_AREA_CM2:g} cm^2, "
        f"y-axis = {CJ_Y_AXIS_SCALE.lower().strip()}"
    )
    plt.grid(True, which="both")
    plt.tight_layout()
    plt.savefig(plot_file, dpi=300)
    print(f"Saved Cj plot to: {plot_file}")

    if SHOW_PLOTS:
        plt.show()

    plt.close()


def set_final_output_state(smu, fg):
    """Leave the instruments on in the requested final state."""

    print("\nLeaving SMU and function generator on in requested final state...")

    if fg is not None:
        safe_write(fg, f"FUNC {FG_WAVEFORM}", "Function generator")
        safe_write(fg, f"VOLT {FINAL_FG_VPP}", "Function generator")
        safe_write(fg, f"VOLT:OFFS {FINAL_FG_OFFSET}", "Function generator")
        safe_write(fg, f"FREQ {FINAL_FG_FREQ_HZ}", "Function generator")
        safe_write(fg, "OUTP ON", "Function generator")
        print(
            f"  Function generator left ON: "
            f"{FINAL_FG_VPP:g} Vpp, {FINAL_FG_FREQ_HZ:g} Hz, "
            f"offset {FINAL_FG_OFFSET:g} V"
        )

    if smu is not None:
        safe_write(smu, f"smua.source.levelv = {FINAL_SMU_VOLTAGE}", "SMU")
        safe_write(smu, "smua.source.output = smua.OUTPUT_ON", "SMU")
        print(f"  SMU left ON: {FINAL_SMU_VOLTAGE:g} V")


# ============================================================
# MAIN PROGRAM
# ============================================================

def main():
    main_sweep_order = MAIN_SWEEP_ORDER.lower().strip()

    if main_sweep_order not in {
        "voltage_sweep_per_frequency",
        "frequency_sweep_per_voltage",
    }:
        raise ValueError(
            'MAIN_SWEEP_ORDER must be "voltage_sweep_per_frequency" or '
            '"frequency_sweep_per_voltage".'
        )

    if abs(SMU_SWEEP_START_VOLTAGE) > MAX_SMU_VOLTAGE:
        raise ValueError("SMU_SWEEP_START_VOLTAGE exceeds MAX_SMU_VOLTAGE.")
    if abs(SMU_SWEEP_STOP_VOLTAGE) > MAX_SMU_VOLTAGE:
        raise ValueError("SMU_SWEEP_STOP_VOLTAGE exceeds MAX_SMU_VOLTAGE.")
    if SMU_CURRENT_LIMIT_A <= 0:
        raise ValueError("SMU_CURRENT_LIMIT_A must be positive.")
    if SET_FINAL_OUTPUT_STATE_AT_END and abs(FINAL_SMU_VOLTAGE) > MAX_SMU_VOLTAGE:
        raise ValueError("FINAL_SMU_VOLTAGE exceeds MAX_SMU_VOLTAGE.")
    if SET_FINAL_OUTPUT_STATE_AT_END and FINAL_FG_VPP <= 0:
        raise ValueError("FINAL_FG_VPP must be positive.")
    if SET_FINAL_OUTPUT_STATE_AT_END and FINAL_FG_FREQ_HZ <= 0:
        raise ValueError("FINAL_FG_FREQ_HZ must be positive.")
    if HIGH_VOLTAGE_MONOTONIC_REL_TOL < 0:
        raise ValueError("HIGH_VOLTAGE_MONOTONIC_REL_TOL must be >= 0.")
    if HIGH_VOLTAGE_MONOTONIC_ABS_TOL_F < 0:
        raise ValueError("HIGH_VOLTAGE_MONOTONIC_ABS_TOL_F must be >= 0.")
    if DEVICE_AREA_CM2 <= 0:
        raise ValueError("DEVICE_AREA_CM2 must be positive to calculate the Cj curve.")
    if FREQUENCY_SPACING_MODE.lower().strip() not in {"log", "linear"}:
        raise ValueError('FREQUENCY_SPACING_MODE must be either "log" or "linear".')
    if ENABLE_VOLTAGE_PRESCAN_FOR_TIME_ESTIMATE:
        if PRESCAN_FG_FREQ_HZ <= 0:
            raise ValueError("PRESCAN_FG_FREQ_HZ must be positive.")
        if PRESCAN_FG_VPP <= 0:
            raise ValueError("PRESCAN_FG_VPP must be positive.")
    if ESTIMATE_READ_OVERHEAD_PER_POINT < 0:
        raise ValueError("ESTIMATE_READ_OVERHEAD_PER_POINT must be >= 0.")
    if ESTIMATE_SAVE_OVERHEAD_PER_OUTER_SWEEP < 0:
        raise ValueError("ESTIMATE_SAVE_OVERHEAD_PER_OUTER_SWEEP must be >= 0.")
    if CV_Y_AXIS_SCALE.lower().strip() not in {"linear", "log"}:
        raise ValueError('CV_Y_AXIS_SCALE must be either "linear" or "log".')
    if CJ_Y_AXIS_SCALE.lower().strip() not in {"linear", "log"}:
        raise ValueError('CJ_Y_AXIS_SCALE must be either "linear" or "log".')

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    detailed_csv_file = OUTPUT_DIR / f"cv_measurements_{main_sweep_order}_{timestamp}.csv"
    cv_csv_file = OUTPUT_DIR / f"cv_curve_{timestamp}.csv"
    prescan_csv_file = OUTPUT_DIR / f"voltage_prescan_{timestamp}.csv"
    cv_plot_file = OUTPUT_DIR / f"cv_curve_{timestamp}.png"
    cj_plot_file = OUTPUT_DIR / f"cj_curve_{timestamp}.png"

    freqs = frequency_points(
        f_start=F_START,
        f_stop=F_STOP,
        spacing_mode=FREQUENCY_SPACING_MODE,
        points_per_decade=POINTS_PER_DECADE,
        linear_frequency_points=LINEAR_FREQUENCY_POINTS,
    )
    smu_points_all = linear_points(
        SMU_SWEEP_START_VOLTAGE,
        SMU_SWEEP_STOP_VOLTAGE,
        SMU_SWEEP_STEP_VOLTAGE,
    )

    rm = None
    dmm = None
    lockin_i = None
    lockin_v = None
    fg = None
    smu = None

    all_frequency_rows = []
    cv_rows = []
    smu_points = []
    saved = False
    plotted = False
    cj_plotted = False
    high_v_filter_applied = False

    try:
        rm = pyvisa.ResourceManager()

        print("Opening VISA instruments...")
        dmm = rm.open_resource(DMM_ADDR)
        lockin_i = rm.open_resource(LOCKIN_I_ADDR)
        lockin_v = rm.open_resource(LOCKIN_V_ADDR)
        fg = rm.open_resource(FG_ADDR)
        smu = rm.open_resource(SMU_ADDR)

        for inst in [dmm, lockin_i, lockin_v, fg, smu]:
            inst.timeout = 10_000
            try:
                inst.write_termination = "\n"
                inst.read_termination = "\n"
            except Exception:
                pass

        print("Configuring DMM...")
        safe_write(dmm, "*RST", "DMM")
        safe_write(dmm, "CONF:VOLT:DC", "DMM")

        print("Configuring SMU...")
        strict_write(smu, "reset()", "SMU")
        strict_write(smu, "smua.source.func = smua.OUTPUT_DCVOLTS", "SMU")
        strict_write(smu, f"smua.source.limiti = {SMU_CURRENT_LIMIT_A}", "SMU")
        strict_write(smu, f"smua.source.levelv = {SMU_SWEEP_START_VOLTAGE}", "SMU")
        strict_write(smu, "smua.source.output = smua.OUTPUT_ON", "SMU")
        time.sleep(SETTLING_TIME_AFTER_SMU)

        print("Configuring function generator...")
        strict_write(fg, "*RST", "Function generator")
        strict_write(fg, f"FUNC {FG_WAVEFORM}", "Function generator")
        strict_write(fg, f"VOLT {VAC_VPP}", "Function generator")
        strict_write(fg, f"VOLT:OFFS {FG_OFFSET}", "Function generator")
        strict_write(fg, f"FREQ {freqs[0]}", "Function generator")
        strict_write(fg, "OUTP ON", "Function generator")

        print("Configuring lock-in #1 for current measurement...")
        for cmd in LOCKIN_I_CONFIG_COMMANDS:
            safe_write(lockin_i, cmd, "Lock-in current")

        print("Configuring lock-in #2 for voltage measurement...")
        for cmd in LOCKIN_V_CONFIG_COMMANDS:
            safe_write(lockin_v, cmd, "Lock-in voltage")

        print("\nStarting CV measurement...")
        print(f"Detailed CSV output: {detailed_csv_file}")
        print(f"CV CSV output:       {cv_csv_file}")
        if ENABLE_VOLTAGE_PRESCAN_FOR_TIME_ESTIMATE and SAVE_PRESCAN_CSV:
            print(f"Pre-scan CSV output: {prescan_csv_file}")
        print(f"CV plot output:      {cv_plot_file}")
        print(f"Cj plot output:      {cj_plot_file}")
        print(f"Device area:         {DEVICE_AREA_CM2:g} cm^2")
        print(f"SMU range:           {SMU_SWEEP_START_VOLTAGE:g} V to {SMU_SWEEP_STOP_VOLTAGE:g} V")
        print(f"SMU step:            {SMU_SWEEP_STEP_VOLTAGE:g} V")
        print(f"Frequency range:     {F_START:g} Hz to {F_STOP:g} Hz")
        print(f"Frequency mode:      {FREQUENCY_SPACING_MODE}")
        if FREQUENCY_SPACING_MODE.lower().strip() == "log":
            print(f"Points per decade:   {POINTS_PER_DECADE:g}")
        else:
            print(f"Linear freq points:  {LINEAR_FREQUENCY_POINTS}")
        print(f"Frequency points:    {len(freqs)}")
        print(f"CV y-axis scale:     {CV_Y_AXIS_SCALE}")
        print(f"Cj y-axis scale:     {CJ_Y_AXIS_SCALE}")

        if ENABLE_VOLTAGE_PRESCAN_FOR_TIME_ESTIMATE:
            prescan_result = voltage_prescan_for_positive_current(
                smu=smu,
                dmm=dmm,
                lockin_i=lockin_i,
                fg=fg,
                smu_points_all=smu_points_all,
                prescan_csv_file=prescan_csv_file,
            )
            first_positive_smu = prescan_result["first_positive_smu_voltage"]
            smu_points = prescan_result["usable_smu_points"]
        else:
            first_positive_smu = find_first_positive_vdc_point(
                smu=smu,
                dmm=dmm,
                lockin_i=lockin_i,
                smu_points=smu_points_all,
            )
            smu_points = [v for v in smu_points_all if v >= first_positive_smu - 1e-12]

        print("\nMeasurement order:")
        print(f"  MAIN_SWEEP_ORDER:      {main_sweep_order}")
        if main_sweep_order == "voltage_sweep_per_frequency":
            print("  Outer loop:            frequency")
            print("  Inner loop:            SMU voltage")
        else:
            print("  Outer loop:            SMU voltage")
            print("  Inner loop:            frequency")
        print(f"  First SMU voltage used: {smu_points[0]:.6g} V")
        print(f"  Last SMU voltage used:  {smu_points[-1]:.6g} V")
        print(f"  Voltage points used:    {len(smu_points)}")
        print(f"  Frequency points used:  {len(freqs)}")
        print(f"  Total tests:            {len(freqs) * len(smu_points)}")

        print_capacitance_time_estimate(
            n_frequencies=len(freqs),
            n_voltage_points=len(smu_points),
            sweep_order=main_sweep_order,
        )

        print("\nStarting capacitance measurements now...")

        if main_sweep_order == "voltage_sweep_per_frequency":
            for frequency_index, f_ac in enumerate(freqs, start=1):
                frequency_rows = voltage_sweep_at_frequency(
                    frequency_index=frequency_index,
                    f_ac=f_ac,
                    smu=smu,
                    dmm=dmm,
                    lockin_i=lockin_i,
                    lockin_v=lockin_v,
                    fg=fg,
                    smu_points=smu_points,
                    total_frequencies=len(freqs),
                )

                all_frequency_rows.extend(frequency_rows)

                if SAVE_PROGRESS_AFTER_EACH_OUTER_SWEEP:
                    save_csv(all_frequency_rows, detailed_csv_file)

        else:
            for voltage_index, smu_voltage in enumerate(smu_points, start=1):
                voltage_rows, ended_by_negative_current = frequency_sweep_at_voltage(
                    voltage_index=voltage_index,
                    smu_voltage=smu_voltage,
                    smu=smu,
                    dmm=dmm,
                    lockin_i=lockin_i,
                    lockin_v=lockin_v,
                    fg=fg,
                    freqs=freqs,
                    total_voltages=len(smu_points),
                )

                all_frequency_rows.extend(voltage_rows)

                if SAVE_PROGRESS_AFTER_EACH_OUTER_SWEEP:
                    save_csv(all_frequency_rows, detailed_csv_file)

                if ended_by_negative_current:
                    print(
                        "\nNegative Idc_pv reached during frequency-sweep-per-voltage mode. "
                        "Stopping remaining higher voltage points and continuing to analysis/plots."
                    )
                    break

        all_frequency_rows = apply_high_voltage_monotonic_filter(all_frequency_rows)
        high_v_filter_applied = True
        save_csv(all_frequency_rows, detailed_csv_file)

        cv_rows = calculate_cv_rows_from_all_measurements(
            all_rows=all_frequency_rows,
            smu_points=smu_points,
        )

        save_csv(all_frequency_rows, detailed_csv_file)
        save_csv(cv_rows, cv_csv_file)
        saved = True

        if MAKE_CV_PLOT:
            plot_cv_curve(cv_rows, cv_plot_file)
            plotted = True

        if MAKE_CJ_PLOT:
            plot_cj_curve(cv_rows, cj_plot_file)
            cj_plotted = True

        print("\nFinal CV/Cj points:")
        for row in cv_rows:
            print(
                f"  Vdc_pv={row['Vdc_pv_median_V']:.6e} V | "
                f"C={format_capacitance_value(row['C_final_median_F'], CAPACITANCE_UNIT)} | "
                f"Cj={row['Cj_final_median_uF_per_cm2']:.6g} uF/cm^2 | "
                f"SMU={row['smu_voltage_V']:.6g} V"
            )

    except StopMeasurement as exc:
        print(f"\nSAFETY STOP: {exc}")

    except KeyboardInterrupt:
        print("\nInterrupted by user.")

    except Exception as exc:
        print(f"\nERROR: {exc}")

    finally:
        if not saved:
            try:
                if all_frequency_rows and not high_v_filter_applied:
                    all_frequency_rows = apply_high_voltage_monotonic_filter(all_frequency_rows)
                    high_v_filter_applied = True

                if all_frequency_rows and smu_points and not cv_rows:
                    cv_rows = calculate_cv_rows_from_all_measurements(
                        all_rows=all_frequency_rows,
                        smu_points=smu_points,
                    )

                save_csv(all_frequency_rows, detailed_csv_file)
                save_csv(cv_rows, cv_csv_file)
                saved = True
            except Exception as exc:
                print(f"WARNING: Could not save CSV files: {exc}")

        if MAKE_CV_PLOT and not plotted and cv_rows:
            try:
                plot_cv_curve(cv_rows, cv_plot_file)
                plotted = True
            except Exception as exc:
                print(f"WARNING: Could not save CV plot: {exc}")

        if MAKE_CJ_PLOT and not cj_plotted and cv_rows:
            try:
                plot_cj_curve(cv_rows, cj_plot_file)
                cj_plotted = True
            except Exception as exc:
                print(f"WARNING: Could not save Cj plot: {exc}")

        if SET_FINAL_OUTPUT_STATE_AT_END:
            set_final_output_state(smu=smu, fg=fg)
        else:
            print("\nClosing instruments without changing output state...")

            if fg is not None and TURN_OFF_FG_AT_END:
                safe_write(fg, "OUTP OFF", "Function generator")

            if smu is not None and TURN_OFF_SMU_AT_END:
                safe_write(smu, "smua.source.output = smua.OUTPUT_OFF", "SMU")

        print("\nClosing VISA instrument handles...")

        for inst in [dmm, lockin_i, lockin_v, fg, smu]:
            if inst is not None:
                try:
                    inst.close()
                except Exception:
                    pass

        if rm is not None:
            try:
                rm.close()
            except Exception:
                pass

        print("Done.")


if __name__ == "__main__":
    main()