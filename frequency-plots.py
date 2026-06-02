import pyvisa
import time
import csv
import math
import re
import matplotlib.pyplot as plt
from datetime import datetime
from pathlib import Path


# ============================================================
# IMPEDANCE FREQUENCY SWEEP AT EITHER MPP OR MANUAL SMU VOLTAGE
# ============================================================
#
# Instruments:
#   DMM          GPIB0::10::INSTR   measures Vdc_pv
#   Lock-in #1   GPIB0::15::INSTR   measures Iac_pv on input A
#                                    and Idc_pv on ADC1
#   Lock-in #2   GPIB0::12::INSTR   measures Vac_pv on A-B differential
#   Function gen GPIB0::14::INSTR   provides the AC perturbation
#   SMU          GPIB0::26::INSTR   sets the DC operating point
#
# Operating-point modes:
#   "MPP_SEARCH":
#       Performs the existing DC SMU sweep, selects the measured maximum
#       power point, and performs an impedance frequency sweep there.
#   "MANUAL_SMU_VOLTAGE":
#       Skips the MPP search entirely. The SMU is set directly to
#       MANUAL_SMU_VOLTAGE and the impedance frequency sweep begins there.
#
# The function-generator AC output is switched ON before enabling the SMU
# output and remains ON throughout the active measurement.
#
# At each accepted frequency point, the program also calculates the parallel
# capacitance model used in the CV script:
#       Y = 1 / Z
#       C = Im(Y) / (2*pi*f)
# The impedance CSV therefore includes admittance and capacitance columns,
# and a capacitance-versus-frequency plot is created.
#
# NOTE ABOUT CURRENT SIGN:
#   IDC_MEASUREMENT_SIGN affects all recorded DC currents and MPP logic.
#   In manual mode, negative DC current is permitted by default because the
#   chosen point may deliberately be away from the power-producing region.
# ============================================================


# ============================================================
# USER SETTINGS
# ============================================================

# Select how the DC operating point is obtained:
#   "MPP_SEARCH"          performs the DC sweep and then measures at MPP.
#   "MANUAL_SMU_VOLTAGE"  skips the MPP search and uses the value below.
OPERATING_POINT_MODE = "MANUAL_SMU_VOLTAGE"
MANUAL_SMU_VOLTAGE = 12.5          # [V], used only in MANUAL_SMU_VOLTAGE mode

# DC SMU sweep used only in MPP_SEARCH mode
SMU_SWEEP_START_VOLTAGE = 11.5     # [V]
SMU_SWEEP_STOP_VOLTAGE = 15.0      # [V]
SMU_SWEEP_STEP_VOLTAGE = 0.1       # [V]
MAX_SMU_VOLTAGE = 15.0             # [V], hard allowed SMU setpoint limit

# The measured DC current is multiplied by this sign before all logic.
# Use +1.0 if positive current corresponds to generated/output current.
# Use -1.0 if the current probe/ADC polarity is reversed.
IDC_MEASUREMENT_SIGN = 1.0

# End-of-sweep rule for the automatic MPP voltage sweep
NEGATIVE_IDC_LIMIT = -1e-6         # [A], MPP sweep ends below this value
REQUIRE_NEGATIVE_CURRENT_ENDPOINT = True

# In manual mode the selected voltage may intentionally give negative Idc.
# Set this True if negative DC current should abort a manual frequency sweep.
ABORT_MANUAL_SWEEP_IF_IDC_NEGATIVE = False

# DC safety limits
SMU_CURRENT_LIMIT_A = 0.5          # [A], SMU compliance/current limit
MAX_IDC_ABS = 2.5                  # [A], abort above this measured magnitude

# Optional measured PV voltage safety abort.
# It is disabled by default because the DC sweep must be allowed to continue
# until Idc_pv becomes negative. Enable only when this is a real safety limit.
STOP_IF_VDC_EXCEEDS_MAX = False
MAX_VDC_PV = 0.80                  # [V]

# Only points meeting these conditions may become the MPP point
MIN_MPP_VDC_PV = 0.0               # [V]
MIN_MPP_IDC_PV = 0.0               # [A]

# Function generator AC perturbation. This remains ON during both the
# optional MPP-search sweep and the impedance frequency sweep.
VAC_VPP = 0.010                    # [Vpp], 10 mVpp
FG_OFFSET = 0.0                    # [V]
FG_WAVEFORM = "SIN"

# Impedance frequency sweep at the selected operating point
F_START = 5                     # [Hz]
F_STOP = 1e4                   # [Hz]
POINTS_PER_DECADE = 4              # logarithmic spacing

# Timing
SETTLING_TIME_AFTER_SMU = 1.0      # [s]
SETTLING_TIME_AFTER_FREQ = 4.0     # [s]
LOCKIN_TIME_CONSTANT_WAIT = 0.0    # [s]

# Minimum usable AC current magnitude
MIN_IAC_MAG = 1e-12                # [A]

# Automatic re-measurement of obvious impedance spikes during the frequency
# sweep. The point is accepted only when |Z'| is within the configured limit.
# Use a limit that is safely above physically expected MPP values for your cell.
REMEASURE_Z_REAL_OUTLIERS = True
MAX_ABS_Z_REAL_OHM = 100.0         # [Ohm], reject if abs(Z') is larger
MAX_OUTLIER_RETRIES = 8            # additional attempts after the first reading
OUTLIER_RETRY_WAIT = 1.0           # [s], same frequency remains applied
ABORT_IF_OUTLIER_RETRIES_EXHAUSTED = True

# Current scaling of ADC1 on lock-in #1
ADC1_TO_AMPERE = 1.0

# Manual phasor inversion settings.
# These alter only AC impedance calculation, not the DC operating point.
INVERT_CURRENT_PHASOR_MANUALLY = False
INVERT_VOLTAGE_PHASOR_MANUALLY = False

# Output
OUTPUT_DIR = Path(".")
SHOW_PLOTS = True
SAVE_MPP_DIAGNOSTIC_PLOT = False       # used only in MPP_SEARCH mode
MAKE_CAPACITANCE_PLOT = True
CAPACITANCE_UNIT = "uF"                # Options: "F", "mF", "uF", "nF"
# Negative C is kept by default because it is useful for diagnosing an AC
# phase/sign convention problem. Set True to hide negative points in the plot.
PLOT_ONLY_POSITIVE_CAPACITANCE = False

# Output shutdown. The FG stays ON throughout all measurement stages, then is
# switched off during cleanup by default. Change TURN_OFF_FG_AT_END to False
# only when the FG must remain active after the program has ended.
TURN_OFF_SMU_AT_END = False
TURN_OFF_FG_AT_END = False


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
# OPTIONAL LOCK-IN CONFIGURATION COMMANDS
# ============================================================

LOCKIN_I_CONFIG_COMMANDS = [
    # Insert exact commands for your Signal Recovery 7225 if required.
]

LOCKIN_V_CONFIG_COMMANDS = [
    # Insert exact commands for your voltage lock-in if required.
]


# ============================================================
# HELPER FUNCTIONS
# ============================================================

FLOAT_RE = re.compile(
    r"[-+]?(?:(?:\d+\.\d*)|(?:\.\d+)|(?:\d+))(?:[Ee][-+]?\d+)?"
)


class StopMeasurement(Exception):
    """Raised when a safety condition requests stopping the measurement."""


def safe_write(inst, cmd, label="instrument"):
    try:
        inst.write(cmd)
    except Exception as exc:
        print(f"WARNING: {label} rejected command {cmd!r}: {exc}")


def strict_write(inst, cmd, label="instrument"):
    try:
        inst.write(cmd)
    except Exception as exc:
        raise RuntimeError(
            f"{label} rejected critical command {cmd!r}: {exc}"
        ) from exc


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

    return [
        10 ** (math.log10(f_start) + k * decades / (n_points - 1))
        for k in range(n_points)
    ]


def linear_points(start, stop, step):
    if step <= 0:
        raise ValueError("SMU_SWEEP_STEP_VOLTAGE must be positive.")
    if stop < start:
        raise ValueError(
            "SMU_SWEEP_STOP_VOLTAGE must be >= SMU_SWEEP_START_VOLTAGE."
        )

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

    return magnitude, wrap_phase_deg(phase_deg)


def invert_phasor(magnitude, phase_deg):
    return abs(magnitude), wrap_phase_deg(phase_deg + 180.0)


def save_csv(rows, csv_file):
    if not rows:
        print(f"No data rows recorded for: {csv_file}")
        return

    fieldnames = list(rows[0].keys())

    with csv_file.open("w", newline="") as file_handle:
        writer = csv.DictWriter(file_handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Saved {len(rows)} rows to: {csv_file}")


def check_hard_dc_safety(vdc_pv, idc_pv):
    """Safety checks that must remain active in both measurement stages."""
    if abs(idc_pv) > MAX_IDC_ABS:
        raise StopMeasurement(
            f"abs(Idc_pv) exceeded limit: {abs(idc_pv):.6e} A "
            f"> {MAX_IDC_ABS:.6e} A"
        )

    if STOP_IF_VDC_EXCEEDS_MAX and vdc_pv > MAX_VDC_PV:
        raise StopMeasurement(
            f"Vdc_pv exceeded limit: {vdc_pv:.6e} V > {MAX_VDC_PV:.6e} V"
        )


def read_dc_values(dmm, lockin_i):
    vdc_pv = safe_query_float(dmm, "READ?", "DMM")
    idc_adc1_raw = safe_query_float(lockin_i, IDC_ADC1_CMD, "Lock-in current ADC1")
    idc_pv = idc_adc1_raw * ADC1_TO_AMPERE * IDC_MEASUREMENT_SIGN
    check_hard_dc_safety(vdc_pv, idc_pv)
    return vdc_pv, idc_pv, idc_adc1_raw


def set_smu_voltage_and_read_dc(smu, dmm, lockin_i, smu_voltage):
    if abs(smu_voltage) > MAX_SMU_VOLTAGE:
        raise StopMeasurement(
            f"Requested SMU voltage {smu_voltage:.6g} V exceeds "
            f"MAX_SMU_VOLTAGE={MAX_SMU_VOLTAGE:.6g} V."
        )

    strict_write(smu, f"smua.source.levelv = {smu_voltage}", "SMU")
    time.sleep(SETTLING_TIME_AFTER_SMU)
    return read_dc_values(dmm, lockin_i)


def read_ac_phasors(lockin_i, lockin_v):
    iac_mag_raw = safe_query_float(
        lockin_i, IAC_MAG_CMD, "Lock-in current magnitude"
    )
    iac_phase_raw = safe_query_float(
        lockin_i, IAC_PHASE_CMD, "Lock-in current phase"
    )
    iac_mag, iac_phase = normalize_signed_phasor(iac_mag_raw, iac_phase_raw)

    if INVERT_CURRENT_PHASOR_MANUALLY:
        iac_mag, iac_phase = invert_phasor(iac_mag, iac_phase)

    vac_mag_raw = safe_query_float(
        lockin_v, VAC_MAG_CMD, "Lock-in voltage magnitude"
    )
    vac_phase_raw = safe_query_float(
        lockin_v, VAC_PHASE_CMD, "Lock-in voltage phase"
    )
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


def impedance_from_mag_phase(vac_mag, vac_phase_deg, iac_mag, iac_phase_deg):
    if iac_mag <= MIN_IAC_MAG:
        raise ValueError(
            f"Iac magnitude {iac_mag:.6e} A is at or below "
            f"MIN_IAC_MAG={MIN_IAC_MAG:.6e} A."
        )

    z_mag = vac_mag / iac_mag
    z_phase_deg = wrap_phase_deg(vac_phase_deg - iac_phase_deg)
    z_phase_rad = math.radians(z_phase_deg)

    z_real = z_mag * math.cos(z_phase_rad)
    z_imag = z_mag * math.sin(z_phase_rad)

    return z_mag, z_phase_deg, z_real, z_imag


def capacitance_from_impedance(z_real, z_imag, frequency_hz):
    """Return parallel-equivalent capacitance from Y = 1/Z and C = Im(Y)/omega."""
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

    raise ValueError("CAPACITANCE_UNIT must be one of: 'F', 'mF', 'uF', 'nF'.")


def format_capacitance_value(value_farad):
    scale, unit_label = capacitance_scale_factor(CAPACITANCE_UNIT)
    return f"{value_farad * scale:.6g} {unit_label}"


# ============================================================
# STAGE 1: DC VOLTAGE SWEEP AND MPP SELECTION
# ============================================================

def dc_voltage_sweep_find_mpp(smu, dmm, lockin_i, smu_points, rows):
    negative_endpoint_reached = False

    print("\nStage 1: DC voltage sweep to locate MPP")
    print("The function-generator AC output remains ON during this stage.")

    for point_index, smu_voltage in enumerate(smu_points, start=1):
        vdc_pv, idc_pv, idc_adc1_raw = set_smu_voltage_and_read_dc(
            smu=smu,
            dmm=dmm,
            lockin_i=lockin_i,
            smu_voltage=smu_voltage,
        )

        pdc_pv = vdc_pv * idc_pv
        is_negative_endpoint = idc_pv < NEGATIVE_IDC_LIMIT
        is_mpp_candidate = (
            vdc_pv >= MIN_MPP_VDC_PV
            and idc_pv >= MIN_MPP_IDC_PV
            and not is_negative_endpoint
        )

        rows.append({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "point_index": point_index,
            "smu_voltage_V": smu_voltage,
            "Vdc_pv_V": vdc_pv,
            "Idc_adc1_raw": idc_adc1_raw,
            "Idc_pv_A": idc_pv,
            "Pdc_pv_W": pdc_pv,
            "is_mpp_candidate": is_mpp_candidate,
            "is_negative_current_endpoint": is_negative_endpoint,
        })

        print(
            f"  point {point_index:>3}/{len(smu_points)} | "
            f"SMU={smu_voltage:>10.6g} V | "
            f"Vdc_pv={vdc_pv:>11.6e} V | "
            f"Idc_pv={idc_pv:>11.6e} A | "
            f"Pdc_pv={pdc_pv:>11.6e} W"
        )

        if is_negative_endpoint:
            negative_endpoint_reached = True
            print(
                f"  DC voltage sweep finished because Idc_pv became negative: "
                f"{idc_pv:.6e} A < {NEGATIVE_IDC_LIMIT:.6e} A."
            )
            break

    candidate_rows = [row for row in rows if row["is_mpp_candidate"]]

    if not candidate_rows:
        raise RuntimeError(
            "No usable MPP candidate was measured. Check IDC_MEASUREMENT_SIGN, "
            "the starting voltage and the measurement connections."
        )

    mpp_row = max(candidate_rows, key=lambda row: row["Pdc_pv_W"])

    print("\nSelected maximum power point:")
    print(
        f"  SMU setpoint = {mpp_row['smu_voltage_V']:.6g} V\n"
        f"  Vdc_pv       = {mpp_row['Vdc_pv_V']:.6e} V\n"
        f"  Idc_pv       = {mpp_row['Idc_pv_A']:.6e} A\n"
        f"  Pdc_pv       = {mpp_row['Pdc_pv_W']:.6e} W"
    )

    return mpp_row, negative_endpoint_reached


def establish_manual_operating_point(smu, dmm, lockin_i, rows):
    """Set and record a manually selected SMU voltage without an MPP search."""
    print("\nStage 1 skipped: using a manual SMU operating-point voltage")
    print(f"  Requested manual SMU setpoint = {MANUAL_SMU_VOLTAGE:.6g} V")

    vdc_pv, idc_pv, idc_adc1_raw = set_smu_voltage_and_read_dc(
        smu=smu,
        dmm=dmm,
        lockin_i=lockin_i,
        smu_voltage=MANUAL_SMU_VOLTAGE,
    )
    pdc_pv = vdc_pv * idc_pv

    if ABORT_MANUAL_SWEEP_IF_IDC_NEGATIVE and idc_pv < NEGATIVE_IDC_LIMIT:
        raise StopMeasurement(
            f"Idc_pv is negative at the manual SMU setpoint: "
            f"{idc_pv:.6e} A < {NEGATIVE_IDC_LIMIT:.6e} A."
        )

    operating_point_row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "point_index": 1,
        "smu_voltage_V": MANUAL_SMU_VOLTAGE,
        "Vdc_pv_V": vdc_pv,
        "Idc_adc1_raw": idc_adc1_raw,
        "Idc_pv_A": idc_pv,
        "Pdc_pv_W": pdc_pv,
        "is_mpp_candidate": False,
        "is_negative_current_endpoint": idc_pv < NEGATIVE_IDC_LIMIT,
        "operating_point_mode": OPERATING_POINT_MODE,
    }
    rows.append(operating_point_row)

    print(
        f"  Manual point: SMU={MANUAL_SMU_VOLTAGE:.6g} V | "
        f"Vdc_pv={vdc_pv:.6e} V | Idc_pv={idc_pv:.6e} A | "
        f"Pdc_pv={pdc_pv:.6e} W"
    )

    return operating_point_row


# ============================================================
# STAGE 2: IMPEDANCE SWEEP AT THE SELECTED OPERATING POINT
# ============================================================

def frequency_sweep_at_operating_point(
    operating_point_row, operating_point_mode, smu, dmm, lockin_i, lockin_v,
    fg, freqs, rows, rejected_outlier_rows,
):
    operating_smu_voltage = operating_point_row["smu_voltage_V"]
    point_name = (
        "selected MPP" if operating_point_mode == "MPP_SEARCH"
        else "manual SMU operating point"
    )
    abort_if_negative_current = (
        operating_point_mode == "MPP_SEARCH"
        or ABORT_MANUAL_SWEEP_IF_IDC_NEGATIVE
    )

    print(f"\nStage 2: Impedance frequency sweep at the {point_name}")
    vdc_before, idc_before, _ = set_smu_voltage_and_read_dc(
        smu=smu,
        dmm=dmm,
        lockin_i=lockin_i,
        smu_voltage=operating_smu_voltage,
    )

    print(
        f"  Established point: SMU={operating_smu_voltage:.6g} V | "
        f"Vdc_pv={vdc_before:.6e} V | Idc_pv={idc_before:.6e} A"
    )

    if abort_if_negative_current and idc_before < NEGATIVE_IDC_LIMIT:
        raise StopMeasurement(
            f"Idc_pv is negative at the {point_name}: "
            f"{idc_before:.6e} A < {NEGATIVE_IDC_LIMIT:.6e} A."
        )

    # The FG was already switched ON before establishing the DC operating
    # point. Only its frequency is changed during the sweep.
    time.sleep(SETTLING_TIME_AFTER_FREQ)

    for point_index, f_ac in enumerate(freqs, start=1):
        strict_write(fg, f"FREQ {f_ac}", "Function generator")
        time.sleep(SETTLING_TIME_AFTER_FREQ + LOCKIN_TIME_CONSTANT_WAIT)

        accepted_row = None
        total_attempts = MAX_OUTLIER_RETRIES + 1
        iac_mag = 0.0

        for attempt_number in range(1, total_attempts + 1):
            vdc_pv, idc_pv, idc_adc1_raw = read_dc_values(dmm, lockin_i)

            if abort_if_negative_current and idc_pv < NEGATIVE_IDC_LIMIT:
                raise StopMeasurement(
                    f"Idc_pv became negative during the {point_name} "
                    f"frequency sweep: {idc_pv:.6e} A < "
                    f"{NEGATIVE_IDC_LIMIT:.6e} A."
                )

            phasors = read_ac_phasors(lockin_i, lockin_v)
            iac_mag = phasors["Iac_mag_corrected_A"]
            iac_phase = phasors["Iac_phase_corrected_deg"]
            vac_mag = phasors["Vac_mag_corrected_V"]
            vac_phase = phasors["Vac_phase_corrected_deg"]

            if iac_mag <= MIN_IAC_MAG:
                print(
                    f"  point {point_index:>3}/{len(freqs)} | "
                    f"f={f_ac:.6g} Hz | attempt {attempt_number}/{total_attempts} | "
                    "Iac too small, skipping this frequency."
                )
                break

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

            is_z_real_outlier = (
                REMEASURE_Z_REAL_OUTLIERS
                and abs(z_real) > MAX_ABS_Z_REAL_OHM
            )

            is_mpp_mode = operating_point_mode == "MPP_SEARCH"
            row = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "operating_point_mode": operating_point_mode,
                "point_index": point_index,
                "measurement_attempt": attempt_number,
                "outlier_retries_before_acceptance": attempt_number - 1,
                "is_rejected_Z_real_outlier": is_z_real_outlier,
                "operating_point_smu_voltage_V": operating_smu_voltage,
                "operating_point_reference_Vdc_pv_V": operating_point_row["Vdc_pv_V"],
                "operating_point_reference_Idc_pv_A": operating_point_row["Idc_pv_A"],
                "operating_point_reference_Pdc_pv_W": operating_point_row["Pdc_pv_W"],
                # Retained for compatibility with older MPP-result processing.
                # They are left blank in manual-voltage mode.
                "mpp_smu_voltage_V": operating_smu_voltage if is_mpp_mode else "",
                "mpp_search_Vdc_pv_V": (
                    operating_point_row["Vdc_pv_V"] if is_mpp_mode else ""
                ),
                "mpp_search_Idc_pv_A": (
                    operating_point_row["Idc_pv_A"] if is_mpp_mode else ""
                ),
                "mpp_search_Pdc_pv_W": (
                    operating_point_row["Pdc_pv_W"] if is_mpp_mode else ""
                ),
                "Vdc_pv_V": vdc_pv,
                "Idc_adc1_raw": idc_adc1_raw,
                "Idc_pv_A": idc_pv,
                "Pdc_pv_W": vdc_pv * idc_pv,
                "f_ac_Hz": f_ac,
                "Vac_mag_raw_V": phasors["Vac_mag_raw_V"],
                "Vac_phase_raw_deg": phasors["Vac_phase_raw_deg"],
                "Vac_mag_corrected_V": vac_mag,
                "Vac_phase_corrected_deg": vac_phase,
                "Iac_mag_raw_A": phasors["Iac_mag_raw_A"],
                "Iac_phase_raw_deg": phasors["Iac_phase_raw_deg"],
                "Iac_mag_corrected_A": iac_mag,
                "Iac_phase_corrected_deg": iac_phase,
                "Z_real_ohm": z_real,
                "Z_imag_ohm": z_imag,
                "Z_magnitude_ohm": z_mag,
                "Z_phase_deg": z_phase,
                "Y_real_S": y_real,
                "Y_imag_S": y_imag,
                "C_uncorrected_F": c_uncorrected,
            }

            if not is_z_real_outlier:
                accepted_row = row
                rows.append(row)
                print(
                    f"  point {point_index:>3}/{len(freqs)} | "
                    f"f={f_ac:>10.6g} Hz | "
                    f"Z'={z_real:>12.6e} ohm | "
                    f"Z''={z_imag:>12.6e} ohm | "
                    f"|Z|={z_mag:>12.6e} ohm | "
                    f"phase={z_phase:>9.4f} deg | "
                    f"C={format_capacitance_value(c_uncorrected)} | "
                    f"attempt={attempt_number}"
                )
                break

            rejected_outlier_rows.append(row)
            print(
                f"  OUTLIER | point {point_index:>3}/{len(freqs)} | "
                f"f={f_ac:>10.6g} Hz | attempt={attempt_number}/{total_attempts} | "
                f"Z'={z_real:.6e} ohm exceeds +/-{MAX_ABS_Z_REAL_OHM:g} ohm"
            )

            if attempt_number < total_attempts:
                print("           Re-measuring this same frequency point...")
                time.sleep(OUTLIER_RETRY_WAIT + LOCKIN_TIME_CONSTANT_WAIT)

        if accepted_row is None and iac_mag > MIN_IAC_MAG:
            message = (
                f"No acceptable impedance result at f={f_ac:.6g} Hz after "
                f"{total_attempts} attempts. All measured values had "
                f"abs(Z') > {MAX_ABS_Z_REAL_OHM:g} ohm."
            )
            if ABORT_IF_OUTLIER_RETRIES_EXHAUSTED:
                raise StopMeasurement(
                    message + " Measurement stopped so a genuine high impedance "
                    "is not silently discarded; increase MAX_ABS_Z_REAL_OHM only "
                    "after checking the setup and expected spectrum."
                )
            print(f"  WARNING: {message} Frequency point skipped.")

    if not rows:
        raise RuntimeError(
            f"No valid impedance points were measured at the {point_name}."
        )

    return rows


# ============================================================
# PLOTTING
# ============================================================

def save_single_frequency_plot(
    impedance_rows,
    y_key,
    ylabel,
    title,
    plot_file,
):
    frequencies = [row["f_ac_Hz"] for row in impedance_rows]
    y_values = [row[y_key] for row in impedance_rows]

    plt.figure(figsize=(8, 5))
    plt.semilogx(frequencies, y_values, marker="o", linestyle="-")
    plt.xlabel("Frequency [Hz]")
    plt.ylabel(ylabel)
    plt.title(title)
    plt.grid(True, which="both")
    plt.tight_layout()
    plt.savefig(plot_file, dpi=300)
    print(f"Saved plot to: {plot_file}")

    if SHOW_PLOTS:
        plt.show()

    plt.close()


def plot_impedance_results(impedance_rows, output_files, point_description):
    save_single_frequency_plot(
        impedance_rows,
        "Z_real_ohm",
        "Z' [Ohm]",
        f"Real impedance at {point_description} versus frequency",
        output_files["z_real_plot"],
    )
    save_single_frequency_plot(
        impedance_rows,
        "Z_imag_ohm",
        "Z'' [Ohm]",
        f"Imaginary impedance at {point_description} versus frequency",
        output_files["z_imag_plot"],
    )
    save_single_frequency_plot(
        impedance_rows,
        "Z_magnitude_ohm",
        "|Z| [Ohm]",
        f"Impedance magnitude at {point_description} versus frequency",
        output_files["z_mag_plot"],
    )
    save_single_frequency_plot(
        impedance_rows,
        "Z_phase_deg",
        "Phase [deg]",
        f"Impedance phase at {point_description} versus frequency",
        output_files["z_phase_plot"],
    )


def plot_capacitance_results(impedance_rows, plot_file, point_description):
    scale, unit_label = capacitance_scale_factor(CAPACITANCE_UNIT)
    valid_rows = []

    for row in impedance_rows:
        frequency = row.get("f_ac_Hz")
        capacitance = row.get("C_uncorrected_F")
        if frequency is None or capacitance is None:
            continue
        if not (math.isfinite(frequency) and math.isfinite(capacitance)):
            continue
        if PLOT_ONLY_POSITIVE_CAPACITANCE and capacitance <= 0:
            continue
        valid_rows.append(row)

    if not valid_rows:
        print("No valid capacitance points available for plotting.")
        return

    valid_rows.sort(key=lambda row: row["f_ac_Hz"])
    frequencies = [row["f_ac_Hz"] for row in valid_rows]
    capacitances = [row["C_uncorrected_F"] * scale for row in valid_rows]

    plt.figure(figsize=(8, 5))
    plt.semilogx(frequencies, capacitances, marker="o", linestyle="-")
    plt.xlabel("Frequency [Hz]")
    plt.ylabel(f"Parallel capacitance [{unit_label}]")
    plt.title(f"Parallel capacitance at {point_description} versus frequency")
    plt.grid(True, which="both")
    plt.tight_layout()
    plt.savefig(plot_file, dpi=300)
    print(f"Saved plot to: {plot_file}")

    if SHOW_PLOTS:
        plt.show()

    plt.close()


def plot_mpp_diagnostic(dc_rows, mpp_row, plot_file):
    voltages = [row["Vdc_pv_V"] for row in dc_rows]
    powers = [row["Pdc_pv_W"] for row in dc_rows]

    plt.figure(figsize=(8, 5))
    plt.plot(voltages, powers, marker="o", linestyle="-")
    plt.plot(
        [mpp_row["Vdc_pv_V"]],
        [mpp_row["Pdc_pv_W"]],
        marker="o",
        linestyle="None",
        label="Selected MPP",
    )
    plt.xlabel("Vdc_pv [V]")
    plt.ylabel("Pdc_pv [W]")
    plt.title("DC power sweep used for MPP selection")
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    plt.savefig(plot_file, dpi=300)
    print(f"Saved diagnostic MPP plot to: {plot_file}")

    if SHOW_PLOTS:
        plt.show()

    plt.close()


# ============================================================
# MAIN PROGRAM
# ============================================================

def main():
    valid_modes = {"MPP_SEARCH", "MANUAL_SMU_VOLTAGE"}
    if OPERATING_POINT_MODE not in valid_modes:
        raise ValueError(
            f"OPERATING_POINT_MODE must be one of {sorted(valid_modes)}, "
            f"not {OPERATING_POINT_MODE!r}."
        )

    if OPERATING_POINT_MODE == "MPP_SEARCH":
        if abs(SMU_SWEEP_START_VOLTAGE) > MAX_SMU_VOLTAGE:
            raise ValueError("SMU_SWEEP_START_VOLTAGE exceeds MAX_SMU_VOLTAGE.")
        if abs(SMU_SWEEP_STOP_VOLTAGE) > MAX_SMU_VOLTAGE:
            raise ValueError("SMU_SWEEP_STOP_VOLTAGE exceeds MAX_SMU_VOLTAGE.")
        initial_smu_voltage = SMU_SWEEP_START_VOLTAGE
        run_prefix = "mpp"
        point_description = "MPP"
    else:
        if abs(MANUAL_SMU_VOLTAGE) > MAX_SMU_VOLTAGE:
            raise ValueError("MANUAL_SMU_VOLTAGE exceeds MAX_SMU_VOLTAGE.")
        initial_smu_voltage = MANUAL_SMU_VOLTAGE
        run_prefix = "manual"
        point_description = f"manual SMU point ({MANUAL_SMU_VOLTAGE:g} V)"

    if SMU_CURRENT_LIMIT_A <= 0:
        raise ValueError("SMU_CURRENT_LIMIT_A must be positive.")
    if IDC_MEASUREMENT_SIGN not in (-1.0, 1.0):
        raise ValueError("IDC_MEASUREMENT_SIGN must be either +1.0 or -1.0.")
    if MAX_ABS_Z_REAL_OHM <= 0:
        raise ValueError("MAX_ABS_Z_REAL_OHM must be positive.")
    if MAX_OUTLIER_RETRIES < 0:
        raise ValueError("MAX_OUTLIER_RETRIES must be zero or positive.")
    if OUTLIER_RETRY_WAIT < 0:
        raise ValueError("OUTLIER_RETRY_WAIT must be zero or positive.")
    capacitance_scale_factor(CAPACITANCE_UNIT)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    dc_file_prefix = (
        "mpp_dc_voltage_sweep"
        if OPERATING_POINT_MODE == "MPP_SEARCH"
        else "manual_dc_operating_point"
    )
    output_files = {
        "dc_csv": OUTPUT_DIR / f"{dc_file_prefix}_{timestamp}.csv",
        "impedance_csv": OUTPUT_DIR / f"{run_prefix}_impedance_frequency_sweep_{timestamp}.csv",
        "rejected_outlier_csv": OUTPUT_DIR / f"{run_prefix}_rejected_impedance_outliers_{timestamp}.csv",
        "mpp_plot": OUTPUT_DIR / f"mpp_diagnostic_power_sweep_{timestamp}.png",
        "z_real_plot": OUTPUT_DIR / f"{run_prefix}_Z_real_vs_frequency_{timestamp}.png",
        "z_imag_plot": OUTPUT_DIR / f"{run_prefix}_Z_imag_vs_frequency_{timestamp}.png",
        "z_mag_plot": OUTPUT_DIR / f"{run_prefix}_Z_magnitude_vs_frequency_{timestamp}.png",
        "z_phase_plot": OUTPUT_DIR / f"{run_prefix}_Z_phase_vs_frequency_{timestamp}.png",
        "capacitance_plot": OUTPUT_DIR / f"{run_prefix}_capacitance_vs_frequency_{timestamp}.png",
    }

    smu_points = None
    if OPERATING_POINT_MODE == "MPP_SEARCH":
        smu_points = linear_points(
            SMU_SWEEP_START_VOLTAGE,
            SMU_SWEEP_STOP_VOLTAGE,
            SMU_SWEEP_STEP_VOLTAGE,
        )
    freqs = logspace_points(F_START, F_STOP, POINTS_PER_DECADE)

    rm = None
    dmm = None
    lockin_i = None
    lockin_v = None
    fg = None
    smu = None

    dc_rows = []
    impedance_rows = []
    rejected_outlier_rows = []
    operating_point_row = None

    try:
        rm = pyvisa.ResourceManager()

        print("Opening VISA instruments...")
        dmm = rm.open_resource(DMM_ADDR)
        lockin_i = rm.open_resource(LOCKIN_I_ADDR)
        lockin_v = rm.open_resource(LOCKIN_V_ADDR)
        fg = rm.open_resource(FG_ADDR)
        smu = rm.open_resource(SMU_ADDR)

        for instrument in [dmm, lockin_i, lockin_v, fg, smu]:
            instrument.timeout = 10_000
            try:
                instrument.write_termination = "\n"
                instrument.read_termination = "\n"
            except Exception:
                pass

        print("Configuring DMM...")
        safe_write(dmm, "*RST", "DMM")
        safe_write(dmm, "CONF:VOLT:DC", "DMM")

        print("Configuring function generator...")
        strict_write(fg, "*RST", "Function generator")
        strict_write(fg, f"FUNC {FG_WAVEFORM}", "Function generator")
        strict_write(fg, f"VOLT {VAC_VPP}", "Function generator")
        strict_write(fg, f"VOLT:OFFS {FG_OFFSET}", "Function generator")
        # The first sweep frequency is applied while the DC point is established.
        strict_write(fg, f"FREQ {freqs[0]}", "Function generator")
        strict_write(fg, "OUTP ON", "Function generator")
        time.sleep(SETTLING_TIME_AFTER_FREQ)

        print("Configuring SMU...")
        strict_write(smu, "reset()", "SMU")
        strict_write(smu, "smua.source.func = smua.OUTPUT_DCVOLTS", "SMU")
        strict_write(smu, f"smua.source.limiti = {SMU_CURRENT_LIMIT_A}", "SMU")
        strict_write(smu, f"smua.source.levelv = {initial_smu_voltage}", "SMU")
        strict_write(smu, "smua.source.output = smua.OUTPUT_ON", "SMU")
        time.sleep(SETTLING_TIME_AFTER_SMU)

        print("Configuring lock-in #1 for current measurement...")
        for cmd in LOCKIN_I_CONFIG_COMMANDS:
            safe_write(lockin_i, cmd, "Lock-in current")

        print("Configuring lock-in #2 for voltage measurement...")
        for cmd in LOCKIN_V_CONFIG_COMMANDS:
            safe_write(lockin_v, cmd, "Lock-in voltage")

        print("\nMeasurement configuration:")
        print(f"  Operating mode:      {OPERATING_POINT_MODE}")
        if OPERATING_POINT_MODE == "MPP_SEARCH":
            print(
                f"  SMU DC sweep:        {SMU_SWEEP_START_VOLTAGE:g} V to "
                f"{SMU_SWEEP_STOP_VOLTAGE:g} V in "
                f"{SMU_SWEEP_STEP_VOLTAGE:g} V steps"
            )
            print(
                f"  Sweep termination:   Idc_pv < {NEGATIVE_IDC_LIMIT:.6e} A"
            )
        else:
            print(f"  Manual SMU voltage:  {MANUAL_SMU_VOLTAGE:g} V")
            print(
                "  Negative Idc abort:   "
                f"{ABORT_MANUAL_SWEEP_IF_IDC_NEGATIVE}"
            )
        print(f"  Frequency sweep:     {F_START:g} Hz to {F_STOP:g} Hz")
        print(f"  Frequency points:    {len(freqs)}")
        print(f"  AC perturbation:     {VAC_VPP:g} Vpp, ON for complete run")
        print(f"  Initial FG freq.:    {freqs[0]:g} Hz")
        print(f"  Capacitance model:   C = Im(1/Z) / (2*pi*f) [{CAPACITANCE_UNIT}]")
        if REMEASURE_Z_REAL_OUTLIERS:
            print(
                f"  Outlier remeasure:   abs(Z') > {MAX_ABS_Z_REAL_OHM:g} Ohm, "
                f"up to {MAX_OUTLIER_RETRIES} retries"
            )

        if OPERATING_POINT_MODE == "MPP_SEARCH":
            operating_point_row, negative_endpoint_reached = dc_voltage_sweep_find_mpp(
                smu=smu,
                dmm=dmm,
                lockin_i=lockin_i,
                smu_points=smu_points,
                rows=dc_rows,
            )
            save_csv(dc_rows, output_files["dc_csv"])

            if SAVE_MPP_DIAGNOSTIC_PLOT:
                plot_mpp_diagnostic(
                    dc_rows, operating_point_row, output_files["mpp_plot"]
                )

            if REQUIRE_NEGATIVE_CURRENT_ENDPOINT and not negative_endpoint_reached:
                raise RuntimeError(
                    "The SMU sweep reached its configured stop voltage before "
                    "Idc_pv became negative. The DC CSV has been saved, but no "
                    "impedance sweep was started. Increase SMU_SWEEP_STOP_VOLTAGE "
                    "within safe limits and run again."
                )
        else:
            operating_point_row = establish_manual_operating_point(
                smu=smu,
                dmm=dmm,
                lockin_i=lockin_i,
                rows=dc_rows,
            )
            save_csv(dc_rows, output_files["dc_csv"])

        frequency_sweep_at_operating_point(
            operating_point_row=operating_point_row,
            operating_point_mode=OPERATING_POINT_MODE,
            smu=smu,
            dmm=dmm,
            lockin_i=lockin_i,
            lockin_v=lockin_v,
            fg=fg,
            freqs=freqs,
            rows=impedance_rows,
            rejected_outlier_rows=rejected_outlier_rows,
        )
        save_csv(impedance_rows, output_files["impedance_csv"])
        if rejected_outlier_rows:
            save_csv(rejected_outlier_rows, output_files["rejected_outlier_csv"])
        plot_impedance_results(impedance_rows, output_files, point_description)
        if MAKE_CAPACITANCE_PLOT:
            plot_capacitance_results(
                impedance_rows, output_files["capacitance_plot"], point_description
            )

        print("\nMeasurement completed successfully.")
        result_label = (
            "Final MPP" if OPERATING_POINT_MODE == "MPP_SEARCH"
            else "Manual operating point"
        )
        print(
            f"{result_label}: SMU={operating_point_row['smu_voltage_V']:.6g} V | "
            f"Vdc_pv={operating_point_row['Vdc_pv_V']:.6e} V | "
            f"Idc_pv={operating_point_row['Idc_pv_A']:.6e} A | "
            f"Pdc_pv={operating_point_row['Pdc_pv_W']:.6e} W"
        )

    except StopMeasurement as exc:
        print(f"\nSAFETY STOP: {exc}")

    except KeyboardInterrupt:
        print("\nInterrupted by user.")

    except Exception as exc:
        print(f"\nERROR: {exc}")

    finally:
        if dc_rows:
            try:
                save_csv(dc_rows, output_files["dc_csv"])
            except Exception as exc:
                print(f"WARNING: Could not save DC operating-point data: {exc}")

        if impedance_rows:
            try:
                save_csv(impedance_rows, output_files["impedance_csv"])
            except Exception as exc:
                print(f"WARNING: Could not save impedance data: {exc}")

        if rejected_outlier_rows:
            try:
                save_csv(rejected_outlier_rows, output_files["rejected_outlier_csv"])
            except Exception as exc:
                print(f"WARNING: Could not save rejected outlier data: {exc}")

        print("\nClosing instruments...")

        if fg is not None and TURN_OFF_FG_AT_END:
            safe_write(fg, "OUTP OFF", "Function generator")

        if smu is not None and TURN_OFF_SMU_AT_END:
            safe_write(smu, "smua.source.output = smua.OUTPUT_OFF", "SMU")

        for instrument in [dmm, lockin_i, lockin_v, fg, smu]:
            if instrument is not None:
                try:
                    instrument.close()
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
