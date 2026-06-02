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
# CV CURVE MEASUREMENT USING FREQUENCY SWEEPS
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
#   1. Find first SMU voltage where measured Vdc_pv is positive.
#   2. At that voltage, run a frequency sweep.
#   3. Calculate capacitance at each frequency.
#   4. Robustly filter out weird frequency points and get one final C value.
#   5. Increase SMU voltage and repeat.
#   6. Save detailed data, save CV data, and plot C versus Vdc_pv.
# ============================================================


# ============================================================
# USER SETTINGS
# ============================================================

# SMU voltage sweep settings
SMU_SWEEP_START_VOLTAGE = 11.0      # [V]
SMU_SWEEP_STOP_VOLTAGE = 15.0      # [V]
SMU_SWEEP_STEP_VOLTAGE = 0.01      # [V]
MAX_SMU_VOLTAGE = 15.0             # [V]

# First positive measured PV voltage threshold
VDC_POSITIVE_THRESHOLD = 1e-4       # [V]

# Stop if measured PV DC voltage becomes too high
MAX_VDC_PV = 0.62                  # [V]
STOP_IF_VDC_EXCEEDS_MAX = True

# SMU current compliance / current limit
SMU_CURRENT_LIMIT_A = 0.5           # [A]

# Function generator AC signal
VAC_VPP = 0.010                    # [Vpp] 10 mVpp
FG_OFFSET = 0.0                    # [V]
FG_WAVEFORM = "SIN"                # sine wave

# Frequency sweep at every voltage point
F_START = 50.0                      # [Hz]
F_STOP = 10000.0                  # [Hz]
POINTS_PER_DECADE = 4             # logarithmic spacing

# Timing
SETTLING_TIME_AFTER_FREQ = 4     # [s]
SETTLING_TIME_AFTER_SMU = 1.0      # [s]
LOCKIN_TIME_CONSTANT_WAIT = 0.0    # [s]

# Safety
MAX_IDC_ABS = 2.5                  # [A]
STOP_IF_IDC_NEGATIVE = True
NEGATIVE_IDC_LIMIT = -1e-6         # [A]

TURN_OFF_SMU_AT_END = True
TURN_OFF_FG_AT_END = True

# Current scaling of ADC1 on lock-in #1
ADC1_TO_AMPERE = 1.0

# Minimum usable AC current
MIN_IAC_MAG = 1e-12                # [A]

# Manual phasor inversion settings
INVERT_CURRENT_PHASOR_MANUALLY = False
INVERT_VOLTAGE_PHASOR_MANUALLY = False

# Output folder
OUTPUT_DIR = Path(".")

# Plot settings
MAKE_CV_PLOT = True
SHOW_PLOTS = True
CAPACITANCE_UNIT = "uF"            # Options: "F", "mF", "uF", "nF"

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
SAVE_PROGRESS_AFTER_EACH_VOLTAGE = True


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
        raise ValueError("F_START and F_STOP must be positive.")
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


def save_csv(rows, csv_file):
    if not rows:
        print(f"\nNo data rows recorded for {csv_file}.")
        return

    fieldnames = list(rows[0].keys())

    with csv_file.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Saved {len(rows)} rows to: {csv_file}")


def check_dc_safety(vdc_pv, idc_pv):
    if STOP_IF_IDC_NEGATIVE and idc_pv < NEGATIVE_IDC_LIMIT:
        raise StopMeasurement(
            f"Idc_pv became negative: {idc_pv:.6e} A < {NEGATIVE_IDC_LIMIT:.6e} A"
        )

    if abs(idc_pv) > MAX_IDC_ABS:
        raise StopMeasurement(
            f"abs(Idc_pv) exceeded limit: {abs(idc_pv):.6e} A > {MAX_IDC_ABS:.6e} A"
        )

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


def frequency_sweep_at_voltage(sweep_index, smu_voltage, smu, dmm, lockin_i, lockin_v, fg, freqs):
    rows = []

    vdc_before, idc_before = set_smu_voltage_and_read_dc(smu, dmm, lockin_i, smu_voltage)
    check_dc_safety(vdc_before, idc_before)

    print(
        f"\nVoltage point {sweep_index}: "
        f"SMU={smu_voltage:.6g} V | Vdc_pv_start={vdc_before:.6e} V | "
        f"Idc_pv_start={idc_before:.6e} A"
    )

    for point_index, f_ac in enumerate(freqs, start=1):
        strict_write(fg, f"FREQ {f_ac}", "Function generator")
        time.sleep(SETTLING_TIME_AFTER_FREQ + LOCKIN_TIME_CONSTANT_WAIT)

        vdc_pv = safe_query_float(dmm, "READ?", "DMM")
        idc_adc1 = safe_query_float(lockin_i, IDC_ADC1_CMD, "Lock-in current ADC1")
        idc_pv = idc_adc1 * ADC1_TO_AMPERE
        check_dc_safety(vdc_pv, idc_pv)

        ph = read_ac_phasors(lockin_i, lockin_v)

        iac_mag = ph["Iac_mag_corrected_A"]
        iac_phase = ph["Iac_phase_corrected_deg"]
        vac_mag = ph["Vac_mag_corrected_V"]
        vac_phase = ph["Vac_phase_corrected_deg"]

        if iac_mag <= MIN_IAC_MAG:
            print(
                f"  point {point_index:>3}/{len(freqs)} | "
                f"f={f_ac:.6g} Hz | WARNING: Iac too small, skipping"
            )
            continue

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
            "sweep_index": sweep_index,
            "smu_voltage_V": smu_voltage,
            "Vdc_pv_start_V": vdc_before,
            "Idc_pv_start_A": idc_before,
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
        }

        rows.append(row)

        print(
            f"  point {point_index:>3}/{len(freqs)} | "
            f"f={f_ac:>10.6g} Hz | "
            f"Vdc={vdc_pv:.6e} V | "
            f"C={format_capacitance_value(c_uncorrected, CAPACITANCE_UNIT)}"
        )

    return rows


def filter_capacitance_rows(rows):
    if not rows:
        return None

    candidate_points = []

    for idx, row in enumerate(rows, start=1):
        f = row.get("f_ac_Hz")
        c = row.get("C_uncorrected_F")

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
    }

    if filter_result and filter_result.get("ok"):
        row.update({
            "C_final_median_F": filter_result["final_median_F"],
            "C_final_mean_F": filter_result["final_mean_F"],
            "C_final_std_F": filter_result["final_std_F"],
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
            "filter_used_points": 0,
            "filter_candidate_points": 0,
            "filter_outlier_points": 0,
            "filter_frequency_min_Hz": float("nan"),
            "filter_frequency_max_Hz": float("nan"),
        })

    return row


def plot_cv_curve(cv_rows, plot_file):
    valid_rows = []

    for row in cv_rows:
        v = row.get("Vdc_pv_median_V")
        c = row.get("C_final_median_F")
        if v is not None and c is not None and math.isfinite(v) and math.isfinite(c):
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
    plt.xlabel("Vdc_pv [V]")
    plt.ylabel(f"Filtered capacitance [{unit_label}]")
    plt.title("CV curve")
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(plot_file, dpi=300)
    print(f"Saved CV plot to: {plot_file}")

    if SHOW_PLOTS:
        plt.show()

    plt.close()


# ============================================================
# MAIN PROGRAM
# ============================================================

def main():
    if abs(SMU_SWEEP_START_VOLTAGE) > MAX_SMU_VOLTAGE:
        raise ValueError("SMU_SWEEP_START_VOLTAGE exceeds MAX_SMU_VOLTAGE.")
    if abs(SMU_SWEEP_STOP_VOLTAGE) > MAX_SMU_VOLTAGE:
        raise ValueError("SMU_SWEEP_STOP_VOLTAGE exceeds MAX_SMU_VOLTAGE.")
    if SMU_CURRENT_LIMIT_A <= 0:
        raise ValueError("SMU_CURRENT_LIMIT_A must be positive.")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    detailed_csv_file = OUTPUT_DIR / f"cv_frequency_sweeps_{timestamp}.csv"
    cv_csv_file = OUTPUT_DIR / f"cv_curve_{timestamp}.csv"
    cv_plot_file = OUTPUT_DIR / f"cv_curve_{timestamp}.png"

    freqs = logspace_points(F_START, F_STOP, POINTS_PER_DECADE)
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
    saved = False
    plotted = False

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
        print(f"CV plot output:      {cv_plot_file}")
        print(f"SMU range:           {SMU_SWEEP_START_VOLTAGE:g} V to {SMU_SWEEP_STOP_VOLTAGE:g} V")
        print(f"SMU step:            {SMU_SWEEP_STEP_VOLTAGE:g} V")
        print(f"Frequency range:     {F_START:g} Hz to {F_STOP:g} Hz")
        print(f"Frequency points:    {len(freqs)}")

        first_positive_smu = find_first_positive_vdc_point(
            smu=smu,
            dmm=dmm,
            lockin_i=lockin_i,
            smu_points=smu_points_all,
        )

        smu_points = [v for v in smu_points_all if v >= first_positive_smu - 1e-12]

        for sweep_index, smu_voltage in enumerate(smu_points, start=1):
            sweep_rows = frequency_sweep_at_voltage(
                sweep_index=sweep_index,
                smu_voltage=smu_voltage,
                smu=smu,
                dmm=dmm,
                lockin_i=lockin_i,
                lockin_v=lockin_v,
                fg=fg,
                freqs=freqs,
            )

            all_frequency_rows.extend(sweep_rows)

            filter_result = filter_capacitance_rows(sweep_rows)
            print_filter_result(filter_result)

            cv_row = create_cv_summary_row(
                sweep_index=sweep_index,
                smu_voltage=smu_voltage,
                sweep_rows=sweep_rows,
                filter_result=filter_result,
            )
            cv_rows.append(cv_row)

            print(
                f"  CV point saved: Vdc_pv={cv_row['Vdc_pv_median_V']:.6e} V | "
                f"C={format_capacitance_value(cv_row['C_final_median_F'], CAPACITANCE_UNIT)}"
            )

            if SAVE_PROGRESS_AFTER_EACH_VOLTAGE:
                save_csv(all_frequency_rows, detailed_csv_file)
                save_csv(cv_rows, cv_csv_file)

        save_csv(all_frequency_rows, detailed_csv_file)
        save_csv(cv_rows, cv_csv_file)
        saved = True

        if MAKE_CV_PLOT:
            plot_cv_curve(cv_rows, cv_plot_file)
            plotted = True

        print("\nFinal CV points:")
        for row in cv_rows:
            print(
                f"  Vdc_pv={row['Vdc_pv_median_V']:.6e} V | "
                f"C={format_capacitance_value(row['C_final_median_F'], CAPACITANCE_UNIT)} | "
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

        print("\nTurning outputs off and closing instruments...")

        if fg is not None and TURN_OFF_FG_AT_END:
            safe_write(fg, "OUTP OFF", "Function generator")

        if smu is not None and TURN_OFF_SMU_AT_END:
            safe_write(smu, "smua.source.output = smua.OUTPUT_OFF", "SMU")

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