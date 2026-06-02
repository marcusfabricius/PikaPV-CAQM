import time
import math
from collections import deque

import pyvisa
import matplotlib.pyplot as plt


# =========================
# Instrument addresses
# =========================

LOCKIN_1_ADDRESS = "GPIB0::12::INSTR"  # A-B differential: A = Vpv-, B = Vpv+
LOCKIN_2_ADDRESS = "GPIB0::15::INSTR"  # A input only, current probe output
FUNCTION_GENERATOR_ADDRESS = "GPIB0::14::INSTR"


# =========================
# Measurement settings
# =========================

MEASUREMENT_FREQUENCY_HZ = 1500.0      # default frequency
AC_SIGNAL_MVPP = 10.0                  # default function generator signal in mVpp

USE_FUNCTION_GENERATOR = True          # set False if you set frequency/Vpp manually

SAMPLE_INTERVAL = 0.2
PLOT_WINDOW = 300
SETTLING_TIME_AFTER_FG_SETUP = 1.0

# Current probe calibration.
# The lock-in measures the current probe output in V RMS.
# If your current probe output is 1 V/A, keep this at 1.0.
# If your current probe output is 100 mV/A, set this to 0.100.
# If your current probe output is 10 mV/A, set this to 0.010.
CURRENT_PROBE_V_PER_A = 1.0

# Flip this to -1 if the calculated capacitance sign is clearly inverted
# because the current probe direction is reversed.
CURRENT_PROBE_SIGN = 1

MIN_VAC_RMS = 1e-9
MIN_IAC_RMS = 1e-12


# =========================
# Helper functions
# =========================

def write_cmd(inst, cmd):
    """Send command to instrument."""
    inst.write(cmd)
    time.sleep(0.05)


def query_float(inst, cmd):
    """
    Query instrument and convert response to float.

    Removes null characters like:
    '0.0E+00\\x00'
    """
    response = inst.query(cmd)

    cleaned = (
        response
        .replace("\x00", "")
        .replace("\r", "")
        .replace("\n", "")
        .strip()
    )

    try:
        return float(cleaned)
    except ValueError:
        raise ValueError(
            f"Could not convert response from command {cmd} to float.\n"
            f"Raw response: {repr(response)}\n"
            f"Cleaned response: {repr(cleaned)}"
        )


def setup_resource(inst):
    """Apply communication settings."""
    inst.timeout = 5000

    # Try "\r" instead if communication is unstable
    inst.write_termination = "\n"
    inst.read_termination = "\n"

    try:
        inst.clear()
    except Exception:
        pass


def wrap_phase_deg(phase_deg):
    """Wrap phase to range -180 to +180 degrees."""
    while phase_deg > 180:
        phase_deg -= 360
    while phase_deg <= -180:
        phase_deg += 360
    return phase_deg


def configure_function_generator(fg, frequency_hz, amplitude_mvpp):
    """
    Configure function generator.

    The function generator gets Vpp.
    The lock-ins read RMS values.
    That is fine, because capacitance is calculated from the measured RMS values.
    """
    amplitude_vpp = amplitude_mvpp / 1000.0

    print("Configuring function generator...")

    try:
        write_cmd(fg, "FUNC SIN")
        write_cmd(fg, f"FREQ {frequency_hz}")
        write_cmd(fg, "VOLT:UNIT VPP")
        write_cmd(fg, f"VOLT {amplitude_vpp}")
        write_cmd(fg, "VOLT:OFFS 0")
        write_cmd(fg, "OUTP ON")
    except Exception as e:
        print(f"Normal SCPI setup failed: {e}")
        print("Trying APPL:SIN setup instead...")

        write_cmd(fg, f"APPL:SIN {frequency_hz},{amplitude_vpp},0")
        write_cmd(fg, "OUTP ON")

    print(
        f"Function generator set to {frequency_hz:.3f} Hz, "
        f"{amplitude_mvpp:.3f} mVpp.\n"
    )


def configure_7225_ab(lockin):
    """
    Configure lock-in at GPIB 12:
    - Voltage input mode
    - A-B differential input
    - External TTL reference input
    - Sensitivity 10 mV
    """

    print("Configuring lock-in 1: GPIB 12, A-B differential...")

    write_cmd(lockin, "IMODE 0")  # Voltage input mode
    write_cmd(lockin, "VMODE 3")  # A-B differential input
    write_cmd(lockin, "IE 1")     # External TTL reference
    write_cmd(lockin, "SEN 21")   # 10 mV sensitivity

    try:
        sensitivity = query_float(lockin, "SEN.")
        print(f"Lock-in 1 sensitivity readback: {sensitivity:.3e} V")
    except Exception as e:
        print(f"Could not read lock-in 1 sensitivity: {e}")

    print("Lock-in 1 configured.\n")


def configure_7225_a_only(lockin):
    """
    Configure lock-in at GPIB 15:
    - Voltage input mode
    - A input only
    - External TTL reference input
    - Sensitivity 10 mV
    """

    print("Configuring lock-in 2: GPIB 15, A input only...")

    write_cmd(lockin, "IMODE 0")  # Voltage input mode
    write_cmd(lockin, "VMODE 1")  # A input only
    write_cmd(lockin, "IE 1")     # External TTL reference
    write_cmd(lockin, "SEN 21")   # 10 mV sensitivity

    try:
        sensitivity = query_float(lockin, "SEN.")
        print(f"Lock-in 2 sensitivity readback: {sensitivity:.3e} V")
    except Exception as e:
        print(f"Could not read lock-in 2 sensitivity: {e}")

    print("Lock-in 2 configured.\n")


def read_lockin_complex(lockin):
    """
    Read lock-in X and Y as a complex phasor.

    Returns:
        complex value = X + jY

    This is better than using MAG. because some lock-ins may return 0 for MAG.
    """
    x_value = query_float(lockin, "X.")

    try:
        y_value = query_float(lockin, "Y.")
    except Exception:
        # Fallback if Y. is not supported
        phase_deg = query_float(lockin, "PHA.")
        cos_phase = math.cos(math.radians(phase_deg))

        if abs(cos_phase) > 0.05:
            magnitude = x_value / cos_phase
        else:
            magnitude = abs(x_value)

        y_value = magnitude * math.sin(math.radians(phase_deg))

    return complex(x_value, y_value)


def calculate_impedance_and_capacitance_complex(v_complex, i_complex, frequency_hz):
    """
    Calculates impedance and parallel capacitance from complex voltage/current.

    Z = V / I
    Y = I / V

    Parallel-equivalent capacitance:
    C = Im(Y) / omega
    """
    v_rms = abs(v_complex)
    i_rms = abs(i_complex)

    if v_rms < MIN_VAC_RMS:
        return None, f"Vac too small: {v_rms:.3e} V RMS"

    if i_rms < MIN_IAC_RMS:
        return None, f"Iac too small: {i_rms:.3e} A RMS"

    omega = 2.0 * math.pi * frequency_hz

    z_complex = v_complex / i_complex
    y_complex = i_complex / v_complex

    capacitance_f = y_complex.imag / omega

    result = {
        "v_rms": v_rms,
        "i_rms": i_rms,

        "z_complex": z_complex,
        "z_mag_ohm": abs(z_complex),
        "z_phase_deg": math.degrees(math.atan2(z_complex.imag, z_complex.real)),
        "z_real_ohm": z_complex.real,
        "z_imag_ohm": z_complex.imag,

        "y_complex": y_complex,
        "y_real_s": y_complex.real,
        "y_imag_s": y_complex.imag,

        "capacitance_f": capacitance_f,
    }

    return result, None


# =========================
# Main program
# =========================

def main():
    rm = pyvisa.ResourceManager()

    lockin_1 = None
    lockin_2 = None
    function_generator = None

    try:
        print("Available VISA resources:")
        print(rm.list_resources())
        print()

        lockin_1 = rm.open_resource(LOCKIN_1_ADDRESS)
        lockin_2 = rm.open_resource(LOCKIN_2_ADDRESS)

        setup_resource(lockin_1)
        setup_resource(lockin_2)

        if USE_FUNCTION_GENERATOR:
            function_generator = rm.open_resource(FUNCTION_GENERATOR_ADDRESS)
            setup_resource(function_generator)

            configure_function_generator(
                function_generator,
                MEASUREMENT_FREQUENCY_HZ,
                AC_SIGNAL_MVPP,
            )

            time.sleep(SETTLING_TIME_AFTER_FG_SETUP)

        configure_7225_ab(lockin_1)
        configure_7225_a_only(lockin_2)

        # Data storage
        times = deque(maxlen=PLOT_WINDOW)

        vac_values = deque(maxlen=PLOT_WINDOW)
        iac_values = deque(maxlen=PLOT_WINDOW)

        z_values = deque(maxlen=PLOT_WINDOW)
        z_phase_values = deque(maxlen=PLOT_WINDOW)

        capacitance_values_uf = deque(maxlen=PLOT_WINDOW)

        # Live plot
        plt.ion()

        fig, (ax_signal, ax_impedance, ax_capacitance) = plt.subplots(
            3,
            1,
            sharex=True
        )

        vac_line, = ax_signal.plot([], [], label="Vac across PV [V RMS]")
        iac_line, = ax_signal.plot([], [], label="Iac through PV [A RMS]")

        z_line, = ax_impedance.plot([], [], label="|Z| [Ohm]")
        z_phase_line, = ax_impedance.plot([], [], label="Z phase [deg]")

        cap_line, = ax_capacitance.plot([], [], label="Capacitance [uF]")

        ax_signal.set_ylabel("Signal")
        ax_impedance.set_ylabel("Impedance")
        ax_capacitance.set_ylabel("Capacitance [uF]")
        ax_capacitance.set_xlabel("Time [s]")

        ax_signal.set_title(
            f"PV capacitance measurement at {MEASUREMENT_FREQUENCY_HZ:.0f} Hz, "
            f"{AC_SIGNAL_MVPP:.1f} mVpp"
        )

        ax_signal.grid(True)
        ax_impedance.grid(True)
        ax_capacitance.grid(True)

        ax_signal.legend()
        ax_impedance.legend()
        ax_capacitance.legend()

        start_time = time.time()

        print("Starting capacitance measurement.")
        print("Press Ctrl+C to stop.\n")

        while True:
            current_time = time.time() - start_time

            # =========================
            # Read voltage lock-in
            # =========================
            # Lock-in 12 raw voltage:
            # raw = A - B = Vpv- - Vpv+
            raw_v_complex = read_lockin_complex(lockin_1)

            # Corrected PV voltage:
            # Vpv = Vpv+ - Vpv- = -raw
            v_complex = -raw_v_complex

            # =========================
            # Read current lock-in
            # =========================
            # Lock-in 15 measures current probe output voltage
            current_probe_complex = read_lockin_complex(lockin_2)

            # Convert current probe voltage to actual current
            i_complex = CURRENT_PROBE_SIGN * current_probe_complex / CURRENT_PROBE_V_PER_A

            # =========================
            # Calculate impedance and capacitance
            # =========================
            result, error = calculate_impedance_and_capacitance_complex(
                v_complex,
                i_complex,
                MEASUREMENT_FREQUENCY_HZ,
            )

            if error is not None:
                print(
                    f"t = {current_time:8.2f} s | "
                    f"{error} | "
                    f"raw LIA12 = {raw_v_complex.real:.3e} + j{raw_v_complex.imag:.3e} V | "
                    f"raw LIA15 = {current_probe_complex.real:.3e} + j{current_probe_complex.imag:.3e} V"
                )

                time.sleep(SAMPLE_INTERVAL)
                continue

            vac_rms = result["v_rms"]
            iac_rms = result["i_rms"]
            capacitance_uf = result["capacitance_f"] * 1e6

            # Store values
            times.append(current_time)

            vac_values.append(vac_rms)
            iac_values.append(iac_rms)

            z_values.append(result["z_mag_ohm"])
            z_phase_values.append(result["z_phase_deg"])

            capacitance_values_uf.append(capacitance_uf)

            # Update plot
            vac_line.set_data(times, vac_values)
            iac_line.set_data(times, iac_values)

            z_line.set_data(times, z_values)
            z_phase_line.set_data(times, z_phase_values)

            cap_line.set_data(times, capacitance_values_uf)

            # Rescale axes
            for ax in (ax_signal, ax_impedance, ax_capacitance):
                ax.relim()
                ax.autoscale_view()

            plt.pause(0.01)

            print(
                f"t = {current_time:8.2f} s | "
                f"Vac = {vac_rms:.6e} V RMS | "
                f"Iac = {iac_rms:.6e} A RMS | "
                f"|Z| = {result['z_mag_ohm']:.6e} Ohm | "
                f"Zphase = {result['z_phase_deg']:8.3f} deg | "
                f"Zreal = {result['z_real_ohm']:.6e} Ohm | "
                f"Zimag = {result['z_imag_ohm']:.6e} Ohm | "
                f"Yreal = {result['y_real_s']:.6e} S | "
                f"Yimag = {result['y_imag_s']:.6e} S | "
                f"C = {capacitance_uf:.6f} uF"
            )

            time.sleep(SAMPLE_INTERVAL)

    except KeyboardInterrupt:
        print("\nMeasurement stopped by user.")

    finally:
        if function_generator is not None:
            try:
                write_cmd(function_generator, "OUTP OFF")
            except Exception:
                pass

            try:
                function_generator.close()
            except Exception:
                pass

        if lockin_1 is not None:
            try:
                lockin_1.close()
            except Exception:
                pass

        if lockin_2 is not None:
            try:
                lockin_2.close()
            except Exception:
                pass

        try:
            rm.close()
        except Exception:
            pass

        print("Connections closed.")


if __name__ == "__main__":
    main()