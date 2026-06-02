import pyvisa
import time
import matplotlib.pyplot as plt
import csv
from datetime import datetime

# ============================================================
# USER SETTINGS
# ============================================================

TARGET_VPV = 1.0          # Stop sweep when Vpv reaches this voltage
START_SMU_VOLTAGE = 11.0    # Starting SMU voltage [V]
SMU_STEP = 0.05            # SMU voltage increment step [V]
SETTLING_TIME = 0.3        # Wait time after changing SMU voltage [s]
MAX_SMU_VOLTAGE = 15       # Safety limit [V]

# ============================================================
# VISA SETUP
# ============================================================

rm = pyvisa.ResourceManager()

dmm = rm.open_resource("GPIB0::10::INSTR")   # Vpv
dmm.timeout = 5000

lockin = rm.open_resource("GPIB0::15::INSTR")  # Ipv (ADC1)
lockin.timeout = 5000

smu = rm.open_resource("GPIB0::26::INSTR")
smu.timeout = 5000

# ============================================================
# CONFIGURE INSTRUMENTS
# ============================================================

dmm.write("*RST")
dmm.write("CONF:VOLT:DC")

smu.write("reset()")
smu.write("smua.source.func = smua.OUTPUT_DCVOLTS")
smu.write(f"smua.source.levelv = {START_SMU_VOLTAGE}")
smu.write("smua.source.output = smua.OUTPUT_ON")

print("\nStarting IV sweep...\n")

# ============================================================
# DATA STORAGE
# ============================================================

smu_data = []
vpv_data = []
ipv_data = []

timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

csv_file = f"IV_curve_{timestamp}.csv"
iv_plot_file = f"IV_curve_{timestamp}.png"
pv_plot_file = f"PV_curve_{timestamp}.png"

# ============================================================
# SWEEP
# ============================================================

smu_voltage = START_SMU_VOLTAGE

try:
    while True:

        smu.write(f"smua.source.levelv = {smu_voltage}")
        time.sleep(SETTLING_TIME)

        vpv = float(dmm.query("READ?"))
        ipv = float(lockin.query("ADC. 1"))  # 1V = 1A

        print(f"SMU: {smu_voltage:.3f} V | Vpv: {vpv:.4f} V | Ipv: {ipv:.6f} A")

        smu_data.append(smu_voltage)
        vpv_data.append(vpv)
        ipv_data.append(ipv)

        # STOP CONDITIONS
        if vpv >= TARGET_VPV:
            print("\nReached target Vpv.")
            break

        if ipv < 0:
            print("\nNegative current detected → stopping sweep.")
            break

        if smu_voltage > MAX_SMU_VOLTAGE:
            print("\nSafety stop: SMU limit reached.")
            break

        smu_voltage += SMU_STEP

except KeyboardInterrupt:
    print("\nInterrupted by user.")

finally:

    # Turn off SMU
    smu.write("smua.source.output = smua.OUTPUT_OFF")

    # ============================================================
    # SAVE CSV
    # ============================================================

    with open(csv_file, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["SMU_V", "Vpv", "Ipv"])
        for s, v, i in zip(smu_data, vpv_data, ipv_data):
            writer.writerow([s, v, i])

    print(f"\nSaved data → {csv_file}")

    # ============================================================
    # IV CURVE
    # ============================================================

    plt.figure()
    plt.plot(vpv_data, ipv_data, marker='o')
    plt.xlabel("Vpv (V)")
    plt.ylabel("Ipv (A)")
    plt.title("IV Curve")
    plt.grid(True)
    plt.xlim(0, TARGET_VPV)
    plt.savefig(iv_plot_file, dpi=300)

    print(f"Saved IV plot → {iv_plot_file}")

    # ============================================================
    # POWER CALCULATION
    # ============================================================

    power_data = [v * i for v, i in zip(vpv_data, ipv_data)]

    max_power = max(power_data)
    max_index = power_data.index(max_power)

    vmp = vpv_data[max_index]
    imp = ipv_data[max_index]

    # ============================================================
    # PRINT MPP
    # ============================================================

    smu_at_mpp = smu_data[max_index]

    print("\n==============================")
    print("MAXIMUM POWER POINT (MPP)")
    print(f"Vmp = {vmp:.4f} V")
    print(f"Imp = {imp:.6f} A")
    print(f"Pmax = {max_power:.6f} W")
    print(f"SMU Voltage at MPP = {smu_at_mpp:.4f} V")
    print("==============================\n")

    # ============================================================
    # PV CURVE
    # ============================================================

    plt.figure()

    plt.plot(vpv_data, power_data, marker='o')

    plt.xlabel("Vpv (V)")
    plt.ylabel("Power (W)")
    plt.title("Power-Voltage Curve")

    plt.grid(True)

    # X-axis
    plt.xlim(0, TARGET_VPV)

    # Y-axis fixed from 0 to 2 W
    plt.ylim(0, 0.1)

    plt.savefig(pv_plot_file, dpi=300)

    print(f"Saved PV plot → {pv_plot_file}")

    plt.show()

    # ============================================================
    # CLEANUP
    # ============================================================

    dmm.close()
    lockin.close()
    smu.close()
    rm.close()

    print("Done.")