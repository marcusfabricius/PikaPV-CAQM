import time
from collections import deque

import pyvisa
import matplotlib.pyplot as plt


# =========================
# Instrument addresses
# =========================

LOCKIN_1_ADDRESS = "GPIB0::12::INSTR"  # A-B differential: A = Vpv-, B = Vpv+
LOCKIN_2_ADDRESS = "GPIB0::15::INSTR"  # A input only

SAMPLE_INTERVAL = 0.2
PLOT_WINDOW = 300


# =========================
# Helper functions
# =========================

def write_cmd(inst, cmd):
    """Send a command to the lock-in amplifier."""
    inst.write(cmd)
    time.sleep(0.05)


def query_float(inst, cmd):
    """
    Query the lock-in and convert response to float.

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


def read_lockin_value_and_phase(lockin):
    """
    Read signed in-phase value and phase.

    X. gives the signed in-phase value in volts.
    PHA. gives phase in degrees.
    """
    value = query_float(lockin, "X.")
    phase = query_float(lockin, "PHA.")
    return value, phase


# =========================
# Main program
# =========================

def main():
    rm = pyvisa.ResourceManager()

    print("Available VISA resources:")
    print(rm.list_resources())
    print()

    lockin_1 = rm.open_resource(LOCKIN_1_ADDRESS)
    lockin_2 = rm.open_resource(LOCKIN_2_ADDRESS)

    setup_resource(lockin_1)
    setup_resource(lockin_2)

    configure_7225_ab(lockin_1)
    configure_7225_a_only(lockin_2)

    # Data storage
    times = deque(maxlen=PLOT_WINDOW)

    value_1_values = deque(maxlen=PLOT_WINDOW)
    phase_1_values = deque(maxlen=PLOT_WINDOW)

    value_2_values = deque(maxlen=PLOT_WINDOW)
    phase_2_values = deque(maxlen=PLOT_WINDOW)

    # Live plot
    plt.ion()

    fig, (ax_value, ax_phase) = plt.subplots(2, 1, sharex=True)

    value_1_line, = ax_value.plot([], [], label="Lock-in 12: corrected Vpv value")
    value_2_line, = ax_value.plot([], [], label="Lock-in 15: A input value")

    phase_1_line, = ax_phase.plot([], [], label="Lock-in 12: phase")
    phase_2_line, = ax_phase.plot([], [], label="Lock-in 15: phase")

    ax_value.set_ylabel("Signed value [V RMS]")
    ax_phase.set_ylabel("Phase [deg]")
    ax_phase.set_xlabel("Time [s]")

    ax_value.set_title("DSP 7225 Lock-in Amplifiers Live Measurement")

    ax_value.grid(True)
    ax_phase.grid(True)

    ax_value.legend()
    ax_phase.legend()

    start_time = time.time()

    print("Starting measurement.")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            current_time = time.time() - start_time

            # Lock-in 12 raw value:
            # raw = A - B = Vpv- - Vpv+
            raw_value_1, phase_1 = read_lockin_value_and_phase(lockin_1)

            # Corrected PV value:
            # Vpv = Vpv+ - Vpv- = -raw
            corrected_vpv_1 = -raw_value_1

            # Because we inverted the sign, the corrected phase is shifted by 180 degrees
            corrected_phase_1 = phase_1 + 180

            # Wrap phase to range -180 to +180 degrees
            if corrected_phase_1 > 180:
                corrected_phase_1 -= 360

            # Lock-in 15 A input only
            value_2, phase_2 = read_lockin_value_and_phase(lockin_2)

            # Store values
            times.append(current_time)

            value_1_values.append(corrected_vpv_1)
            phase_1_values.append(corrected_phase_1)

            value_2_values.append(value_2)
            phase_2_values.append(phase_2)

            # Update plot
            value_1_line.set_data(times, value_1_values)
            value_2_line.set_data(times, value_2_values)

            phase_1_line.set_data(times, phase_1_values)
            phase_2_line.set_data(times, phase_2_values)

            # Rescale axes
            ax_value.relim()
            ax_value.autoscale_view()

            ax_phase.relim()
            ax_phase.autoscale_view()

            plt.pause(0.01)

            print(
                f"t = {current_time:8.2f} s | "
                f"LIA 12 Vpv = {corrected_vpv_1:.6e} V RMS | "
                f"LIA 12 phase = {corrected_phase_1:8.3f} deg | "
                f"LIA 15 value = {value_2:.6e} V RMS | "
                f"LIA 15 phase = {phase_2:8.3f} deg"
            )

            time.sleep(SAMPLE_INTERVAL)

    except KeyboardInterrupt:
        print("\nMeasurement stopped by user.")

    finally:
        lockin_1.close()
        lockin_2.close()
        rm.close()
        print("Connections closed.")


if __name__ == "__main__":
    main()