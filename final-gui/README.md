# MeasureApp Web GUI

Local browser GUI for solar-cell DC, impedance, C-V, and live lock-in measurements.

The app is a Flask web interface around the measurement backend in `gui-v1.py`. It is intended to run on the measurement PC connected to the instruments over VISA/GPIB.

## Files

Required files for the GUI:

- `web_app.py` - starts the local Flask web server and API.
- `gui-v1.py` - measurement backend and instrument control.
- `default_settings.yaml` - editable default values for Advanced settings.
- `templates/index.html` - browser UI.
- `static/app.js` - UI logic, plotting, live view, polling, upload/download.
- `static/app.css` - UI styling.
- `requirements.txt` - Python dependencies.
- `measurement_output/` - generated CSV files.

## Install

From this folder:

```powershell
cd final-gui
python -m pip install -r requirements.txt
```

For real instruments, PyVISA also needs a working VISA backend installed on the PC, for example NI-VISA.

## Start

```powershell
cd final-gui
python web_app.py
```

Open:

```text
http://127.0.0.1:5000
```

The terminal running `web_app.py` shows measurement progress, current frequency/voltage points, capacitance values, safety stops, output file paths, and errors.

## Configuration

Edit `default_settings.yaml` to change startup defaults. Restart `python web_app.py` after editing it.

GPIB addresses can be written as only the number:

```yaml
dmm_addr: 10
lockin_i_addr: 15
lockin_v_addr: 12
fg_addr: 14
smu_addr: 26
```

The app expands these to `GPIB0::<number>::INSTR`.

Important settings:

- `auto_smu_range` - automatically calibrates SMU start/stop for the solar cell.
- `auto_smu_step_by_speed` - automatically chooses SMU points from the measured `Vdc_pv` spacing when Automatic SMU range is enabled.
- `smu_start_v`, `smu_stop_v` - manual search/sweep limits when automatic range is off, and the search envelope for calibration.
- `smu_step_v` - DC and MPP search step size used when automatic step size is off.
- `cv_smu_step_v` - SMU voltage step size used for Complete AC / C-V when automatic step size is off.
- `freq_start_hz`, `freq_stop_hz` - default AC frequency range.
- `vac_vpp`, `fg_offset_v`, `fg_waveform` - function generator settings.
- `settling_after_smu_s`, `settling_after_freq_s`, `lockin_time_constant_wait_s` - timing settings.
- `max_smu_v`, `smu_current_limit_a`, `max_idc_abs_a`, `max_vdc_pv_v` - safety limits.
- `idc_adc1_to_ampere`, `idc_measurement_sign` - conversion/sign for ADC1 current.
- `simulation_mode` - use fake instruments for UI/backend testing without lab hardware.

## Automatic SMU Range Calibration

When `auto_smu_range` is enabled, the GUI can calibrate the solar-cell SMU voltage range.

The calibration:

1. Starts inside the configured `smu_start_v` to `smu_stop_v` envelope.
2. Uses a coarse scan to find where `Vdc_pv` first becomes positive.
3. Uses a coarse scan to find where `Idc_pv` becomes negative.
4. Refines those boundaries down to about `0.005 V`.
5. Verifies the detected start and stop points.
6. Stores only the calibrated `smu_start_v` and `smu_stop_v`.

The calibration precision is only for finding the range. It does not overwrite `smu_step_v` or `cv_smu_step_v`.

## Automatic SMU Step Size

`auto_smu_step_by_speed` is enabled by default and only becomes active when `auto_smu_range` is also enabled.

When active, the GUI greys out `smu_step_v` and `cv_smu_step_v`. Instead of stepping the SMU by a fixed voltage, the backend searches for the next SMU voltage that makes `Vdc_pv` increase by the target amount for the selected speed:

- Fast: `0.05 Vdc_pv`
- Medium: `0.025 Vdc_pv`
- Slow: `0.01 Vdc_pv`

This is used for I-V/P-V voltage sweeps, MPP searches, and C-V voltage points. The search measurements between accepted points are used only to find the next SMU voltage; the saved curve points are the accepted roughly evenly spaced `Vdc_pv` points.

During calibration, only Stop is shown. Resume is hidden because there is no useful plot-selection screen to resume into.

If a first measurement is started while automatic range is enabled and no calibration exists yet, the app calibrates first and then continues with the selected measurement.

## Measurements

### Standard DC Measurement

Runs a DC sweep from `smu_start_v` to `smu_stop_v`. With automatic SMU step size on, points are spaced by `Vdc_pv`; otherwise the sweep uses `smu_step_v`.

Recorded variables:

- `Vdc_pv`
- `Idc_pv`
- `Pdc_pv`
- `V_SMU`

Default result plots:

- I-V
- P-V

The I-V and P-V plots are shown in the positive quadrant only: `0+ V` and `0+ A/W`.

After the sweep, the backend finds the maximum power point from the measured DC data and saves it in the results. When the run exits, the SMU is left on at `smu_stop_v`.

### Standard Frequency Sweep

Measures impedance over a log-spaced frequency range.

Operating point options:

- MPP search - first does a DC sweep, selects the maximum power point, then runs the frequency sweep there. With automatic SMU step size on, the MPP sweep is spaced by `Vdc_pv`; otherwise it uses `smu_step_v`.
- Manual SMU voltage - uses `manual_smu_voltage_v` directly.

Recorded variables:

- `frequency`
- `Z_real`
- `Z_imag`
- `Z_mag`
- `Phase_Z`
- `C`
- `Vac_pv`
- `Iac_pv`
- `Phase_Vac`
- `Phase_Iac`

The final DC operating point is also saved and shown:

- `Vdc_pv`
- `Idc_pv`
- `Pdc_pv`
- `V_SMU`

Default result plots:

- Z_real over frequency
- Z_imag over frequency
- Z_mag over frequency
- Phase_Z over frequency

The terminal also prints capacitance for each frequency point.

### Complete AC Measurement

Runs a C-V style measurement. For each SMU voltage point, it measures one or more AC frequencies and calculates impedance/capacitance.

With automatic SMU step size on, voltage points are spaced by `Vdc_pv`. Otherwise voltage points use `cv_smu_step_v`.

Frequency mode options:

- Frequency range - uses `freq_start_hz` to `freq_stop_hz`.
- Single frequency - uses one exact frequency.

Recorded variables:

- `frequency`
- `Z_real`
- `Z_imag`
- `Z_mag`
- `Phase_Z`
- `C`
- `Vac_pv`
- `Iac_pv`
- `Phase_Vac`
- `Phase_Iac`
- `Vdc_pv`
- `Idc_pv`
- `Pdc_pv`

Default result plot:

- C-V

There is also a default option for C over frequency at a selected `Vdc_pv`. Because the exact requested voltage may not exist in the data, the GUI uses the closest measured `Vdc_pv`.

### Live Lock-In Data

Streams live values from lock-in 12 and lock-in 15.

Shown values:

- `Vpv_dc`
- `Vpv_ac`
- `Vpv phase`
- `Ipv_ac`
- `Ipv phase`

Live controls:

- SMU voltage
- Function generator frequency

Changing either field and pressing Enter applies the change without closing the live popup.

Live plots:

- Vpv_ac and Ipv_ac over time
- Vpv phase and Ipv phase over time

Very small values are shown in scientific notation.

## Output Files

CSV files are written to `measurement_output/` by default.

Common output files:

- `iv_pv_sweep_*.csv`
- `frequency_dc_operating_point_*.csv`
- `frequency_impedance_sweep_*.csv`
- `frequency_rejected_impedance_outliers_*.csv`
- `cv_curve_*.csv`
- `cv_frequency_sweeps_*.csv`
- `cv_rejected_impedance_outliers_*.csv`
- `ab_differential_live_*.csv`
- `smu_range_calibration_*.csv`
- `combined_<mode>_*.csv`

Use Download combined CSV on the results screen to download the combined dataset for the latest run.

CSV files can also be imported through Advanced settings. The GUI tries to detect the correct measurement mode and loads the matching default plots.

## Stop, Resume, and Running Measurements

Stop requests a safe stop. The backend finishes the current safe operation, leaves outputs in the intended lab state, and closes instrument connections.

If the browser page is closed while a measurement is running, reopen the GUI and press Resume to return to the active measurement screen.

If another measurement is started while one is already running, the GUI shows an in-page popup with options to keep the current run or stop it and start the new one.

## Intentional Output Behavior

The function generator output is left on after runs.

The SMU output is also left on after runs. When the backend knows the SMU stop voltage, it sets the SMU to `smu_stop_v` before going idle so the solar-cell current is at the lowest expected point.

If Automatic SMU range calibration is enabled and has completed, `smu_stop_v` is the calibrated stop voltage. Otherwise it is the value from Advanced settings / `default_settings.yaml`.

This behavior is intentional for the lab workflow. Check the instrument front panels before disconnecting or changing hardware.

## Safety Behavior

The backend checks:

- SMU voltage against `max_smu_v`.
- Absolute DC current against `max_idc_abs_a`.
- Optional PV voltage limit against `max_vdc_pv_v`.
- Optional negative-current stop against `negative_idc_limit_a`.
- Minimum AC current magnitude against `min_iac_mag_a`.

If a safety limit is hit, the measurement stops and the terminal prints the reason.

## Troubleshooting

No instruments found:

- Check VISA installation.
- Check GPIB cable/controller.
- Check addresses in `default_settings.yaml`.

Wrong current sign:

- Adjust `idc_measurement_sign`.
- Check `idc_adc1_cmd` and `idc_adc1_to_ampere`.

Automatic calibration cannot find boundaries:

- Make sure `smu_start_v` and `smu_stop_v` cover the expected solar-cell range.
- Check DMM voltage polarity and ADC1 current sign.
- Check `vdc_positive_threshold_v` and `negative_idc_limit_a`.

Plots show no compatible data:

- Confirm the selected plot variables exist in the loaded CSV.
- For uploaded CSVs, use files created by this GUI when possible.
- For C over frequency at `Vdc_pv`, enter a target voltage that is inside the measured range.

The terminal is too noisy:

- Normal `/api/status` polling logs are suppressed. Measurement progress logs are kept.
