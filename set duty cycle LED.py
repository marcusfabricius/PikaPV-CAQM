import pyvisa

# Initialize VISA resource manager
rm = pyvisa.ResourceManager()

# Connect to the instrument at GPIB address 14
instrument = rm.open_resource('GPIB0::14::INSTR')

# Optional: print ID to confirm connection
print(instrument.query('*IDN?'))

# Reset the instrument (optional but recommended)
instrument.write('*RST')

# Set function to Pulse
instrument.write('FUNC PULS')

# Set amplitude to 5V (Vpp)
instrument.write('VOLT 5')

# Set DC offset to 2.5V
instrument.write('VOLT:OFFS 2.5')

# Set pulse width to 10 microseconds
instrument.write('PULS:WIDT 10E-6')

# Optional: set frequency (needed to define repetition rate)
# For example, 1 kHz:
instrument.write('FREQ 1E3')

# Turn output ON
instrument.write('OUTP ON')

print("Pulse signal configured successfully.")

# Close connection when done
instrument.close()
rm.close()