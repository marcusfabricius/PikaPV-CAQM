import pyvisa
import time

rm = pyvisa.ResourceManager()
inst = rm.open_resource('GPIB0::14::INSTR')

print(inst.query('*IDN?'))

inst.write('*RST')
inst.write('FUNC PULS')
inst.write('VOLT 5')
inst.write('VOLT:OFFS 2.5')

freq = 1e3
period = 1 / freq

inst.write(f'FREQ {freq}')
inst.write('OUTP ON')

duty_min = 1
duty_max = 99
step = 2
delay = 0.05

try:
    while True:

        # Up sweep
        for duty in range(duty_min, duty_max + 1, step):
            width = (duty / 100) * period
            inst.write(f'PULS:WIDT {width}')
            time.sleep(delay)

        # Down sweep
        for duty in range(duty_max, duty_min - 1, -step):
            width = (duty / 100) * period
            inst.write(f'PULS:WIDT {width}')
            time.sleep(delay)

except KeyboardInterrupt:
    print("Stopped")

finally:
    inst.write('OUTP OFF')
    inst.close()
    rm.close()