# PikaPV Installer Project

This repository packages PikaPV as a self-contained desktop application for:

- Windows: `PikaPV-Setup-<version>.exe`
- macOS: `PikaPV-macOS-<version>.dmg`
- Debian/Ubuntu Linux: `PikaPV_<version>_<architecture>.deb`
- Linux portable bundle: `PikaPV-Linux-<version>-<architecture>.tar.gz`

Starting PikaPV launches its local server and opens the browser interface.
Python, Flask, Waitress, PyVISA, templates, and static assets are bundled.

## Important Hardware Limitation

PyVISA is bundled, but the operating-system-specific VISA library and
GPIB-to-USB driver are not. Install compatible drivers separately on every
measurement computer. On macOS/Linux, the app can also fall back to a Python-based
backend when `pyvisa-py` and `gpib-ctypes` are installed in the same environment.

- Windows commonly uses NI-VISA or Keysight IO Libraries.
- Linux requires a supported VISA backend and may require device permissions or
  udev rules.
- macOS requires VISA and GPIB drivers that support the exact macOS version and
  CPU architecture.

Hardware support can differ significantly between operating systems. Verify all
instruments on the target computer before using a packaged release.

## Repository Layout

```text
PikaPV-installer/
|-- src/                         Directly runnable PikaPV source
|-- packaging/
|   |-- PikaPV.spec              Windows/Linux PyInstaller onedir bundle
|   |-- PikaPV-macos.spec        macOS application bundle
|   `-- PikaPV.iss               Windows Inno Setup installer
|-- .github/workflows/           Native builds for all platforms
|-- build.cmd / build.ps1        Windows build
|-- build-macos.sh               macOS build
|-- build-linux.sh               Linux build
|-- run-dev.cmd / run-dev.ps1    Windows source runner
|-- check-source.cmd             Source validation without starting PikaPV
|-- requirements-build.txt
`-- VERSION
```

Generated files under `build`, `dist`, and `installer-output` are ignored by
Git.

## Native Builds Required

PyInstaller does not cross-compile application bundles. Build each installer on
its target operating system:

- Build Windows installers on Windows.
- Build macOS installers on macOS.
- Build Linux packages on Linux.

The included GitHub Actions workflows do this automatically on native runners.

## Run From Source

The files under `src` remain directly runnable before packaging.

### Windows

```powershell
.\run-dev.cmd -InstallDependencies
```

### macOS or Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r src/requirements.txt
cd src
python web_app.py
```

Running from source stores settings and measurement output inside `src`. PikaPV
opens at `http://127.0.0.1:5000`.

Terminal output appears in the console that started the program. On macOS and
Linux, if the application is launched from a desktop icon or GUI without an
attached terminal, PikaPV now attempts to open a native terminal window and tail
`logs/pikapv.log` for runtime messages.

If you see an error about `linux-gpib` or `gpib-ctypes`, install those packages
and ensure your GPIB adapter driver is available. On macOS, a native VISA/GPIB
driver may still be required even if `pyvisa-py` is installed.

Packaged builds also write log messages to `logs/pikapv.log` under the app data
directory (`%LOCALAPPDATA%/CAQM/PikaPV` on Windows,
`~/Library/Application Support/CAQM/PikaPV` on macOS, or
`$XDG_DATA_HOME/CAQM/PikaPV` on Linux).

PikaPV configures the LED generator during startup. For development without
instruments, set `simulation_mode: true` in `src/default_settings.yaml` before
starting the application.

## Validate Without Starting PikaPV

Windows:

```powershell
.\check-source.cmd
```

macOS or Linux:

```bash
python3 -m py_compile src/web_app.py src/pikapv_backend.py
```

These commands do not open the browser or connect to instruments.

## Build Windows

Requirements:

- Windows 10 or Windows 11, 64-bit
- 64-bit Python 3.13 recommended
- Inno Setup 6

Build the installer:

```powershell
.\build.cmd -Clean
```

Build only the portable PyInstaller folder:

```powershell
.\build.cmd -Clean -SkipInstaller
```

Outputs:

```text
dist/PikaPV/PikaPV.exe
installer-output/PikaPV-Setup-<version>.exe
```

## Build macOS

Requirements:

- macOS
- Python 3.13 recommended
- Apple command-line tools providing `hdiutil`, `ditto`, and optional
  `codesign`

Build:

```bash
bash build-macos.sh
```

Outputs:

```text
dist/macos/PikaPV.app
installer-output/PikaPV-macOS-<version>.dmg
installer-output/PikaPV-macOS-<version>.zip
```

The local build is unsigned by default. For a signed app:

```bash
PIKAPV_CODESIGN_IDENTITY="Developer ID Application: Example" bash build-macos.sh
```

Public distribution normally also requires Apple notarization. The project does
not automatically notarize because that requires private Apple credentials.

## Build Linux

Requirements:

- Debian or Ubuntu-based Linux
- Python 3.13 recommended
- `python3-venv`, `dpkg-deb`, and `tar`

Build:

```bash
bash build-linux.sh
```

Outputs:

```text
installer-output/PikaPV_<version>_<architecture>.deb
installer-output/PikaPV-Linux-<version>-<architecture>.tar.gz
```

Install the Debian package:

```bash
sudo apt install ./installer-output/PikaPV_<version>_<architecture>.deb
```

Start it from the application menu or run:

```bash
pikapv
```

## Rebuild After Changes

1. Edit files under `src`.
2. Validate syntax without starting PikaPV.
3. Run and verify the source version using simulation or intended hardware.
4. Update the semantic version in `VERSION`.
5. Build natively on each required operating system.
6. Test every generated installer on its target measurement computer.

Do not edit generated files under `build`, `dist`, or `installer-output`.

## Installed Data Locations

Packaged applications preserve editable settings, uploads, logs, and
measurement output outside the installed application:

```text
Windows:
%LOCALAPPDATA%\CAQM\PikaPV

macOS:
~/Library/Application Support/CAQM/PikaPV

Linux:
${XDG_DATA_HOME:-~/.local/share}/CAQM/PikaPV
```

The application log is stored at `logs/pikapv.log` inside that directory.
Existing user settings are preserved during application upgrades.

Set `PIKAPV_DATA_DIR` before starting PikaPV to use another writable directory.

## GitHub Actions

The included workflows build on native hosted runners when manually started or
when pushing a tag such as `v0.2.0`:

- `build-windows.yml`: Windows `.exe` installer
- `build-macos-linux.yml`: macOS `.dmg`/`.zip` and Linux `.deb`/`.tar.gz`

Artifacts are uploaded to the workflow run. GitHub Actions cannot test the VISA
backend or physical instruments, so every release still requires validation on
the actual target measurement computers.

## Release Checklist

- Verify GPIB addresses and safety defaults.
- Verify source mode and simulation mode.
- Confirm the target OS has a compatible VISA/GPIB driver.
- Verify the packaged application discovers all instruments.
- Confirm output and logs are written to the expected user-data directory.
- Confirm Stop behavior and intentional SMU/function-generator output behavior.
- Archive the tested installers and matching source revision.

