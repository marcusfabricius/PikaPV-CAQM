import pyvisa
import time
import matplotlib.pyplot as plt
import csv
from datetime import datetime

# ============================================================
# USER SETTINGS
# ============================================================

TARGET_VPV = 0.627          # Stop sweep when Vpv reaches this voltage
START_SMU_VOLTAGE = 6.0    # Starting SMU voltage [V]
SMU_STEP = 0.1            # SMU voltage increment step [V]
SETTLING_TIME = 0.3        # Wait time after changing SMU voltage [s]
MAX_SMU_VOLTAGE = 12.8       # Safety limit [V]

# ============================================================
# VISA CONNECTIONS
# ============================================================

rm = pyvisa.ResourceManager()

# Keithley 2000 Multimeter (measures Vpv)
dmm = rm.open_resource("GPIB0::10::INSTR")
dmm.timeout = 5000

# Lock-in Amplifier 7260 (ADC1 -> Ipv)
lockin = rm.open_resource("GPIB0::15::INSTR")
lockin.timeout = 5000

# Keithley 2651A SMU
smu = rm.open_resource("GPIB0::26::INSTR")
smu.timeout = 5000

# ============================================================
# CONFIGURE INSTRUMENTS
# ============================================================

# Multimeter setup
dmm.write("*RST")
dmm.write("CONF:VOLT:DC")

# SMU setup
smu.write("reset()")
smu.write("smua.source.func = smua.OUTPUT_DCVOLTS")

# Set initial SMU voltage
smu.write(f"smua.source.levelv = {START_SMU_VOLTAGE}")

# Turn SMU output ON
smu.write("smua.source.output = smua.OUTPUT_ON")

print("\nStarting IV sweep...")
print(f"Initial SMU voltage = {START_SMU_VOLTAGE:.3f} V")
print(f"Target Vpv = {TARGET_VPV:.3f} V")
print("Sweep will stop if current becomes negative.")
print("Press Ctrl+C to stop.\n")

# ============================================================
# DATA STORAGE
# ============================================================

vpv_data = []
ipv_data = []
smu_data = []

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

csv_filename = f"IV_curve_data_{timestamp}.csv"
plot_filename = f"IV_curve_plot_{timestamp}.png"

# ============================================================
# SWEEP LOOP
# ============================================================

smu_voltage = START_SMU_VOLTAGE

try:

    while True:

        # ----------------------------------------------------
        # Set SMU voltage
        # ----------------------------------------------------
        smu.write(f"smua.source.levelv = {smu_voltage}")

        time.sleep(SETTLING_TIME)

        # ----------------------------------------------------
        # Read Vpv from multimeter
        # ----------------------------------------------------
        vpv = float(dmm.query("READ?"))

        # ----------------------------------------------------
        # Read Ipv from ADC1
        # 1 V = 1 A
        # ----------------------------------------------------
        ipv = float(lockin.query("ADC. 1"))

        # ----------------------------------------------------
        # Log values
        # ----------------------------------------------------
        print(
            f"SMU: {smu_voltage:7.3f} V | "
            f"Vpv: {vpv:7.4f} V | "
            f"Ipv: {ipv:9.6f} A"
        )

        # Store data
        smu_data.append(smu_voltage)
        vpv_data.append(vpv)
        ipv_data.append(ipv)

        # ----------------------------------------------------
        # STOP CONDITIONS
        # ----------------------------------------------------

        # Stop when Vpv target is reached
        if vpv >= TARGET_VPV:
            print("\nReached target Vpv.")
            break

        # Stop when current becomes negative
        if ipv < 0:
            print("\nNegative current detected. Stopping sweep.")
            break

        # ----------------------------------------------------
        # Increase SMU voltage slowly
        # ----------------------------------------------------
        smu_voltage += SMU_STEP

        # Safety limit
        if smu_voltage > MAX_SMU_VOLTAGE:
            print("\nSafety stop: maximum SMU voltage reached.")
            break

except KeyboardInterrupt:
    print("\nMeasurement interrupted by user.")

finally:

    # ========================================================
    # TURN OFF SMU
    # ========================================================

    smu.write("smua.source.output = smua.OUTPUT_OFF")

    # ========================================================
    # SAVE CSV DATA
    # ========================================================

    with open(csv_filename, "w", newline="") as file:
        writer = csv.writer(file)

        writer.writerow(["SMU Voltage (V)", "Vpv (V)", "Ipv (A)"])

        for s, v, i in zip(smu_data, vpv_data, ipv_data):
            writer.writerow([s, v, i])

    print(f"\nSaved data to: {csv_filename}")

    # ========================================================
    # CREATE IV PLOT
    # ========================================================

    plt.figure(figsize=(8, 6))

    plt.plot(vpv_data, ipv_data, marker='o')

    plt.xlabel("Vpv (V)")
    plt.ylabel("Ipv (A)")
    plt.title("Solar Panel IV Curve")
    plt.grid(True)

    plt.xlim(0, TARGET_VPV)

    plt.savefig(plot_filename, dpi=300)

    print(f"Saved IV plot to: {plot_filename}")

    plt.show()

    # ========================================================
    # CLOSE CONNECTIONS
    # ========================================================

    dmm.close()
    lockin.close()
    smu.close()
    rm.close()

    print("\nMeasurement complete.")