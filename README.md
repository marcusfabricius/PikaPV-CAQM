# PikaPV Web GUI

PikaPV is a local browser interface for solar-cell DC, impedance, C-V, and live
lock-in measurements. It controls the laboratory instruments through VISA/GPIB,
opens automatically in the browser, and saves measurement results as CSV files.

## Installation

### 1. Connect the instruments

1. Connect all instruments to the same GPIB bus.
2. Connect the GPIB-to-USB controller to any connector on that bus.
3. Connect the USB side to the measurement computer.
4. Turn on all instruments and wait until they have fully booted.
5. Verify the solar-cell wiring and probe polarity.

Each instrument must have a unique GPIB address. The default addresses are:

| Instrument | Address |
|---|---:|
| DMM | 10 |
| LED function generator | 11 |
| Voltage lock-in amplifier | 12 |
| AC function generator | 14 |
| Current lock-in amplifier | 15 |
| SMU | 26 |

### 2. Install Python

Install Python 3 and ensure `python` or `python3` is available from the terminal:

```text
python --version
```

On Windows, enable **Add Python to PATH** during Python installation.

### 3. Install VISA/GPIB drivers

Install a VISA implementation and the driver for the connected GPIB-to-USB
controller. PyVISA alone cannot communicate with instruments without a working
system VISA backend.

Common options include:

- NI-VISA
- Keysight IO Libraries Suite
- A compatible Linux or macOS VISA backend

Use the vendor's connection utility to confirm that all instruments can be
discovered before starting PikaPV.

### 4. Install Python packages

The launcher automatically checks and installs missing packages from
`src/requirements.txt`.

To install them manually:

```text
python -m pip install -r src/requirements.txt
```

On Linux or macOS, use `python3` instead of `python` when required.

## Starting PikaPV

Run the launcher for your operating system from the project folder.

### Windows

Double-click:

```text
Windows_start_web_gui.bat
```

Or run:

```powershell
.\Windows_start_web_gui.bat
```

### Linux

Make the launcher executable once:

```bash
chmod +x Linux_start_web_gui.sh
```

Then run:

```bash
./Linux_start_web_gui.sh
```

### macOS

Make the launcher executable once:

```bash
chmod +x MacOS_start_web_gui.command
```

Then double-click `MacOS_start_web_gui.command` or run:

```bash
./MacOS_start_web_gui.command
```

macOS may require approval in **System Settings > Privacy & Security** the first
time the launcher is opened.

### Manual start

The same cross-platform launcher can be started directly:

```text
python start_web_gui.py
```

The launcher:

1. Checks required Python packages.
2. Installs missing packages when possible.
3. Starts `src/web_app.py`.
4. Opens the browser at `http://127.0.0.1:5000`.

Keep the terminal open while using PikaPV. It shows measurement progress,
instrument warnings, safety stops, file locations, and errors.

## Measurements

| Mode | Purpose |
|---|---|
| Standard DC | Measures I-V and P-V curves and determines the MPP. |
| Standard frequency sweep | Measures impedance over frequency at MPP or a manual operating point. |
| Complete AC | Measures impedance and capacitance over DC voltage points. |
| Live lock-in data | Streams live DC/AC values and allows live SMU, frequency, and brightness control. |

Every non-live measurement first calibrates the useful solar-cell SMU range.
Live mode starts without calibration.

## Configuration

- `default_settings.yaml`: instrument addresses, safety limits, signs, commands,
  and measurement defaults.
- `speedprofile_settings.yaml`: Custom, Fast, Medium, and Slow timing, point
  density, and AC averaging settings.

Both files contain comments explaining every option. Restart PikaPV after
editing `default_settings.yaml`.

## Output

Measurement CSV files are written to `measurement_output/`. The Results screen
can download the combined CSV and each displayed plot separately.

The SMU, AC function generator, and LED function generator intentionally remain
on after measurements. The SMU is left at the calibrated stop voltage.

## Project Files

```text
start_web_gui.py              Cross-platform Python launcher
Windows_start_web_gui.bat     Windows launcher
Linux_start_web_gui.sh        Linux launcher
MacOS_start_web_gui.command   macOS launcher
default_settings.yaml         Measurement and instrument defaults
speedprofile_settings.yaml    Speed profiles
src/web_app.py                Web server and API
src/pikapv-backend.py         Measurement and instrument control
src/templates/                Browser interface
src/static/                   Interface logic, styling, and assets
```

## Troubleshooting

- **No instruments found:** check power, GPIB addresses, cables, and VISA
  drivers.
- **Calibration fails:** check the solar-cell connection, polarity, illumination,
  and configured calibration range.
- **Incorrect current sign:** check probe orientation and current sign settings.
- **Unstable AC readings:** check shielding, grounding, lock-in sensitivity, and
  settling times.
- **Browser does not open:** visit `http://127.0.0.1:5000` manually.

Before disconnecting hardware, inspect the instrument front panels and place all
outputs in a safe state.
