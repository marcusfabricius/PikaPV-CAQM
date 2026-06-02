import pyvisa
import time

# ----------------------------------------
# VISA setup
# ----------------------------------------
rm = pyvisa.ResourceManager()

# Keithley 2000 Multimeter (GPIB address 10)
dmm = rm.open_resource("GPIB0::10::INSTR")
dmm.timeout = 5000

# Lock-in Amplifier 7260 (GPIB address 15)
lockin = rm.open_resource("GPIB0::15::INSTR")
lockin.timeout = 5000

# ----------------------------------------
# Configure Keithley
# ----------------------------------------
dmm.write("*RST")
dmm.write("CONF:VOLT:DC")

print("Logging measurements...")
print("Press Ctrl+C to stop.\n")

try:
    while True:
        # Read Keithley voltage
        voltage = float(dmm.query("READ?"))

        # Read ADC1 from lock-in amplifier
        # 1 V = 1 A
        current = float(lockin.query("ADC. 1"))

        # Print results
        print(
            f"Keithley Voltage: {voltage:.6f} V | "
            f"Current (ADC1): {current:.6f} A"
        )

        time.sleep(1)

except KeyboardInterrupt:
    print("\nStopping logging...")

finally:
    dmm.close()
    lockin.close()
    rm.close()