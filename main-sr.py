import pyvisa
import time

rm = pyvisa.ResourceManager()

# Use GPIB address 16
lockin = rm.open_resource('GPIB0::12::INSTR')

# Check communication
print(lockin.query('*IDN?'))

# Reset instrument
lockin.write('*RST')
time.sleep(1)

# Set to external reference (Ref In A)
lockin.write('IE 1')

# Normal lock-in mode
lockin.write('VMODE 0')

# Time constant (100 ms)
lockin.write('TC 4')

# Sensitivity (adjust if needed)
lockin.write('SEN 10')

# Wait for stabilization
time.sleep(2)

# Read values
x = lockin.query('X.')
y = lockin.query('Y.')
r = lockin.query('MAG.')
theta = lockin.query('PHA.')

print(f"X: {x}")
print(f"Y: {y}")
print(f"R: {r}")
print(f"Phase: {theta}")

lockin.close()