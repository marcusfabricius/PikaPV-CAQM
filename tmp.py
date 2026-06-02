import pyvisa

rm = pyvisa.ResourceManager()

inst = rm.open_resource("GPIB0::15::INSTR")

inst.timeout = 3000
inst.write_termination = '\n'
inst.read_termination = '\n'

print("CONNECTED")

# Test common commands
commands = [
    "*IDN?",
    "MAG.",
    "MAG",
    "PHA.",
    "PHA",
    "OUTP? 1",
    "OUTP? 2",
    "OUTP? 3",
    "OUTP? 4"
]

for cmd in commands:
    try:
        response = inst.query(cmd)
        print(f"{cmd} -> {response}")
    except Exception as e:
        print(f"{cmd} FAILED: {e}")