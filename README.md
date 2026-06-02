# MeasureApp

MeasureApp is a Python/Tkinter measurement GUI for a solar-cell impedance and capacitance setup. It combines the earlier standalone IV/PV, CV, impedance frequency sweep, and A-B differential lock-in monitor scripts into one GUI-driven program.

The main application file is `gui-v1.py`.

## What It Measures

MeasureApp can acquire and plot:

- IV curve
- PV curve
- CV curve
- Impedance frequency plots
- Z' versus frequency
- Z'' versus frequency
- |Z| versus frequency
- Phase versus frequency
- Nyquist plot
- Capacitance versus frequency
- A-B differential lock-in live monitor

The GUI also supports custom plotting after a run. Select a dataset, choose numeric X and Y columns, and choose linear or logarithmic axes.

## Hardware

The code controls instruments through PyVISA/GPIB. Default addresses are:

| Instrument | Default address | Purpose |
| --- | --- | --- |
| DMM | `GPIB0::10::INSTR` | Measures DC PV voltage, `Vdc_pv` |
| Lock-in current amplifier | `GPIB0::15::INSTR` | Measures AC current and DC current through `ADC1` |
| Lock-in voltage amplifier | `GPIB0::12::INSTR` | Measures AC voltage with A-B differential input |
| Function generator | `GPIB0::14::INSTR` | Provides small AC perturbation |
| SMU | `GPIB0::26::INSTR` | Sets the DC operating point |

Do not change these addresses unless the lab setup has changed.

## Requirements

Install Python packages:

```powershell
pip install pyvisa matplotlib
```

Tkinter is also required. It is included with most standard Python installations on Windows.

For real measurements, the measurement PC must also have a working VISA backend and the correct GPIB drivers installed. Use simulation mode if the instruments are not connected.

## Running

From this directory:

```powershell
python gui-v1.py
```

The GUI opens as `MeasureApp`.

Before using real hardware, check:

- GPIB addresses
- SMU voltage range and current limit
- DC current scaling from lock-in `ADC1`
- DC current sign, `+1` or `-1`
- AC perturbation amplitude
- Lock-in command compatibility
- Safety limits

## Main Files

| File | Purpose |
| --- | --- |
| `gui-v1.py` | Main combined GUI application |
| `main-iv-pv-curve.py` | Earlier standalone IV/PV sweep script |
| `main-cv-pv-curve.py` | Earlier standalone CV sweep script |
| `frequency-plots.py` | Earlier standalone impedance/frequency sweep script |
| `A-B differential.py` | Earlier standalone A-B differential monitor |
| `A-B differential-beta.py` | Earlier A-B monitor with impedance/capacitance calculations |
| `gpib-searcher.py` | Prints available VISA resources |
| `main-keithley-smu.py`, `main-multimeter-keithley2000.py`, `main-sr.py`, `main.py`, `set duty cycle LED.py`, `tmp.py` | Small instrument test or development scripts |
| `measurement_output/` | Default output folder for the GUI |
| `Data/`, `Hello World DATA/` | Existing measurement data and generated plots |

## Architecture

`gui-v1.py` is organized around four main classes:

### `Settings`

Stores user-configurable settings from the GUI:

- VISA/GPIB addresses
- SMU sweep limits
- voltage and current safety limits
- function generator settings
- frequency range
- lock-in commands
- CV/frequency speed settings
- outlier handling
- plotting units
- simulation mode

### `VisaController`

Handles direct instrument communication:

- opens and closes VISA resources
- configures the DMM, SMU, function generator, and lock-ins
- reads DC values
- reads AC phasors
- applies DC safety checks
- shuts down outputs at the end of a run when configured

### `MeasurementEngine`

Contains measurement logic:

- `run_pre_scan()`
- `run_iv_pv()`
- `run_cv()`
- `run_frequency_sweep()`
- `run_ab_monitor()`
- `measure_impedance_point()`
- `filter_capacitance_rows()`
- `dc_voltage_sweep_find_mpp()`

### `MeasureApp`

Builds and runs the Tkinter GUI:

- collects settings from input fields
- starts measurements in a background thread
- handles the Stop button
- updates the log window
- stores datasets from completed runs
- plots built-in and custom plots with embedded Matplotlib

Keep these responsibilities separate when changing the code. Measurement logic should stay in `MeasurementEngine`, instrument communication should stay in `VisaController`, and GUI callbacks should only collect settings, start or stop workers, display logs, and plot data.

## Measurement Flows

### 1. Pre-Measure Scan

The pre-scan is a fast SMU voltage sweep. It records `Vdc_pv`, `Idc_pv`, and power, then estimates:

- the first positive `Vdc_pv` point
- the expected CV voltage range
- the number of CV voltage points
- estimated duration for Fast, Medium, and Slow CV modes

Output:

- `pre_scan_*.csv`

### 2. IV/PV Sweep

The SMU voltage is swept from start to stop. At each point:

- the DMM reads `Vdc_pv`
- lock-in `ADC1` reads DC current
- the app calculates `Pdc_pv_W = Vdc_pv * Idc_pv`

The sweep stops when:

- target `Vpv` is reached
- `Idc_pv` becomes negative, if enabled
- a configured safety limit is hit
- the user presses Stop

Output:

- `iv_pv_sweep_*.csv`

### 3. CV Sweep

The app first finds the first positive `Vdc_pv` point, or uses the last pre-scan result. It then steps through SMU voltages. At each voltage:

- it performs a frequency sweep
- reads AC voltage/current magnitude and phase
- calculates impedance, admittance, and capacitance
- filters bad capacitance points
- stores one final capacitance value per voltage

Outputs:

- `cv_frequency_sweeps_*.csv`
- `cv_curve_*.csv`
- `cv_rejected_impedance_outliers_*.csv`, when outliers are recorded

### 4. Frequency Sweep

Frequency sweep has two operating point modes:

- `MPP_SEARCH`: first performs a DC sweep and selects the maximum power point
- `MANUAL_SMU_VOLTAGE`: directly uses the configured manual SMU voltage

Then it performs an impedance frequency sweep at that operating point.

Outputs:

- `frequency_dc_operating_point_*.csv`
- `frequency_impedance_sweep_*.csv`
- `frequency_rejected_impedance_outliers_*.csv`, when outliers are recorded

### 5. A-B Differential Monitor

The monitor continuously reads `X.` and `PHA.` from both lock-ins until Stop is pressed.

Lock-in 12 is corrected because the raw A-B signal gives `Vpv- - Vpv+`; the code flips the sign and shifts phase by 180 degrees.

Output:

- `ab_differential_live_*.csv`

## Speed Levels

The GUI has three speed modes:

| Mode | Points per decade | Repeats | Behavior |
| --- | ---: | ---: | --- |
| Fast | 2 | 1 | Fewer points and shorter settling |
| Medium | 4 | 2 | Balanced default-style mode |
| Slow | 8 | 4 | More points, more repeats, longer settling |

These mainly affect CV and frequency sweeps.

## Calculations

Impedance is calculated from:

- AC voltage magnitude
- AC voltage phase
- AC current magnitude
- AC current phase

The app calculates:

- `Z_magnitude_ohm`
- `Z_phase_deg`
- `Z_real_ohm`, Z'
- `Z_imag_ohm`, Z''
- admittance `Y = 1 / Z`
- `Y_real_S`
- `Y_imag_S`
- capacitance from `Im(Y) / (2 * pi * f)`

Capacitance values are stored internally in farads. The GUI can display capacitance in F, mF, uF, or nF.

## Outlier Handling

The app can re-measure obvious impedance outliers where `abs(Z_real_ohm)` exceeds the configured threshold.

Important GUI settings:

- `Remeasure Z' outliers`
- `Max |Z'| before retry [ohm]`
- `Outlier retries`
- `Abort if Z' retries fail`

Current behavior:

- If retries eventually produce an acceptable point, the accepted point is stored.
- If retries fail and abort is disabled, the last measured point is kept, marked with `accepted_after_outlier_retries_exhausted`, and given a `measurement_warning`.
- If retries fail and abort is enabled, the run stops.

This prevents a single suspicious high `Z'` point from killing the whole GUI run by default.

## Safety Behavior

The app checks:

- maximum SMU voltage
- maximum absolute DC current
- optional negative DC current stop condition
- optional maximum `Vdc_pv` stop condition
- minimum AC current magnitude
- Stop button state

Important current settings:

- `Max |Idc| safety [A]`, default `10 A`
- `Idc ADC1 to ampere`
- `Idc sign (+1 or -1)`
- `Stop when |Idc| exceeds max`

A previous failure happened because `abs(Idc_pv)` was about `4.416 A` while an older hardcoded limit was `2.5 A`. The limit is now configurable in the GUI.

## Simulation Mode

`gui-v1.py` includes `FakeInstrument` and `FakeResourceManager`.

Enable `Simulation mode` in the GUI to test:

- GUI layout
- logging
- measurement flow
- output CSV generation
- plotting

Simulation mode does not control real hardware.

## Output Data

The default GUI output directory is:

```text
measurement_output/
```

Output filenames include timestamps, for example:

```text
iv_pv_sweep_20260601_170702.csv
frequency_impedance_sweep_20260601_170702.csv
```

The GUI does not automatically save every plot as PNG during measurement. Use `Save current plot as PNG` to save the currently displayed Matplotlib figure.

## Instrument Command Notes

The lock-in and function generator commands may be model-specific. Current commands include:

- `MAG.`
- `PHA.`
- `ADC. 1`
- `X.`
- `SEN 21`
- `IMODE 0`
- `VMODE 3`
- `VMODE 1`
- `IE 1`

Function generator command `VOLT 0.010` is intended as a 10 mVpp perturbation, but this should be checked on the actual generator.

## Important Lab Cautions

- Do not assume `ADC1` current scaling is correct; it depends on the current probe.
- Do not assume current sign is correct; it may need `+1` or `-1`.
- Do not blindly lower the current safety limit if the setup normally measures several amps.
- Do not change GPIB addresses unless the lab setup changed.
- Do not assume lock-in commands work on every model.
- Confirm whether the function generator interprets `VOLT` as Vpp, Vrms, or another amplitude convention.
- The code has been syntax-checked during development, but still needs full validation on the real hardware.

## Development Guidelines

When fixing or extending the app:

- Keep settings in the `Settings` dataclass.
- Keep calculations and helper functions near the top of `gui-v1.py`.
- Keep direct instrument communication inside `VisaController`.
- Keep measurement procedures inside `MeasurementEngine`.
- Keep GUI callbacks inside `MeasureApp` focused on settings, threading, logs, and plots.
- Avoid adding measurement logic directly inside GUI callbacks.
- Preserve simulation mode when changing measurement logic.
- Be conservative with safety-related defaults.

## Quick Hardware Discovery

To list VISA resources:

```powershell
python gpib-searcher.py
```

Use the result to confirm the expected GPIB instruments are visible before running a real measurement.
