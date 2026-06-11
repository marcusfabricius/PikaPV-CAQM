# PikaPV Web GUI

Local browser GUI for solar-cell DC, impedance, C-V, and live lock-in measurements.

The app is a Flask web interface whose runtime files now live under `src/`, with the measurement backend in `src/pikapv-backend.py`. It is intended to run on the measurement PC connected to the instruments over VISA/GPIB.

## Files

Required files for the GUI:

- `src/web_app.py` - starts the local Flask web server and API.
- `src/pikapv-backend.py` - measurement backend and instrument control.
- `default_settings.yaml` - editable default values for Advanced settings.
- `src/templates/index.html` - browser UI.
- `src/static/app.js` - UI logic, plotting, live view, polling, upload/download.
- `src/static/app.css` - UI styling.
- `src/requirements.txt` - Python dependencies.
- `measurement_output/` - generated CSV files.

## Start

### Run using the launcher script (recommended)

Use the launcher script for your OS:

- Windows: `Windows_start_web_gui.bat`
- macOS: `MacOS_start_web_gui.command`
- Linux: `Linux_start_web_gui.sh`

### Manual start via terminal

If you prefer to start the app directly from a terminal:

```powershell
cd final-gui
python start_web_gui.py
```

It should automatically open:

```text
http://127.0.0.1:5000
```

The terminal running `start_web_gui.py` shows measurement progress, current frequency/voltage points, capacitance values, safety stops, output file paths, and errors.

## Configuration
 
Edit `default_settings.yaml` to change startup defaults. Restart `python web_app.py` after editing it.

GPIB addresses can be written as only the number:

```yaml
dmm_addr: 10
lockin_i_addr: 15
lockin_v_addr: 12
fg_addr: 14
led_fg_addr: 11
smu_addr: 26
```

The app expands these to `GPIB0::<number>::INSTR`.

Important settings:

- `auto_smu_range` - always enabled in the web GUI because every non-live measurement starts with an automatic SMU range calibration.
- `auto_smu_step_by_speed` - automatically chooses SMU points from the measured `Vdc_pv` spacing when Automatic SMU range is enabled.
- `smu_start_v`, `smu_stop_v` - the search envelope used by the automatic calibration.
- `smu_step_v` - DC and MPP search step size used when automatic step size is off.
- `cv_smu_step_v` - SMU voltage step size used for Complete AC / C-V when automatic step size is off.
- `freq_start_hz`, `freq_stop_hz` - default AC frequency range.
- `vac_vpp`, `fg_offset_v`, `fg_waveform` - function generator settings.
- `led_duty_cycle_percent` - Tektronix AFG3101 LED modulation duty cycle, clamped to `1-99%`.
- `settling_after_smu_s`, `settling_after_freq_s`, `lockin_time_constant_wait_s` - timing settings.
- `max_smu_v`, `smu_current_limit_a`, `max_idc_abs_a`, `max_vdc_pv_v` - safety limits.
- `idc_adc1_to_ampere`, `idc_measurement_sign` - conversion/sign for ADC1 current.
- `simulation_mode` - use fake instruments for UI/backend testing without lab hardware.

## Automatic SMU Range Calibration

Every Standard DC, Standard Frequency Sweep, and Complete AC measurement starts by calibrating the solar-cell SMU voltage range. Live Lock-In Data starts immediately without calibration.

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

Accepted SMU voltages are cached during the current calibrated run and can be reused by later voltage-sweep stages within that run. The next non-live measurement recalibrates and clears the previous voltage-point cache.

The bottom progress bar includes extra estimated time for this first uncached automatic SMU search, plus a small fixed overhead allowance for instrument commands, CSV writing, and UI/backend bookkeeping.

During calibration, only Stop is shown. Resume is hidden because there is no useful plot-selection screen to resume into.

After calibration completes, the selected measurement continues automatically.

## LED Modulation Generator

The GUI configures the Tektronix AFG3101 at `led_fg_addr` for LED modulation whenever a measurement session starts:

- Pulse waveform
- Frequency `1 MHz`
- Amplitude `5 Vpp`
- Offset `2.5 V`
- Output on
- Duty cycle from Advanced settings, `1-99%`

The LED generator is configured once when `web_app.py` starts. The LED duty cycle can be changed in Advanced settings under `LED settings` using either the slider or textbox; changes are applied directly to the generator.

The LED brightness slider and speed-profile selector are also available directly on Screen 1. Their values stay synchronized with Advanced settings.

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

Runs a C-V style measurement. For each SMU voltage point, it measures one or more AC frequencies and calculates the complex impedance.

With automatic SMU step size on, voltage points are spaced by `Vdc_pv`. Otherwise voltage points use `cv_smu_step_v`.

Frequency mode options:

- Frequency range - uses `freq_start_hz` to `freq_stop_hz`.
- Single frequency - uses one exact frequency.

PikaPV calculates capacitance directly from the measured complex impedance using `C = Im(1/Z) / w`. Detailed frequency rows save this value as `C_uncorrected_F`. For each C-V voltage point, the program rejects capacitance outliers across the measured frequencies and saves their filtered median as the final `C`. A single-frequency measurement uses the capacitance from that frequency.

At each frequency, PikaPV takes the number of AC phasor samples configured by the selected speed profile. It calculates complex impedance for each sample, rejects samples outside the configured maximum spread around the median complex impedance, and saves the median accepted `Z_real` and `Z_imag`. If fewer than a majority of the requested samples are accepted, the frequency point is retried using the normal outlier-retry settings. The CSV records requested/accepted/rejected sample counts and measured spread.

The speed-profile YAML contains separate AC sample count, spread tolerance, and sample interval settings for Frequency sweep and CV curve. Custom profile values can also be edited in Advanced settings and are saved back to the YAML.

For custom plots of a frequency-dependent value over `Vdc_pv`, enter a target frequency. PikaPV selects the closest measured frequency for every voltage point and combines repeated readings at that frequency using their median.

For custom frequency-domain plots such as `Z_mag`, `Z_real`, `Z_imag`, `Phase_Z`, or `C` over frequency, enter a target `Vdc_pv`. PikaPV selects the voltage sweep whose median measured `Vdc_pv` is closest to the requested voltage. This closest-voltage selection also applies to plots between two frequency-dependent values, such as a custom Nyquist plot.

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

For non-live measurements, `smu_stop_v` is the stop voltage found by the calibration performed at the start of that measurement.

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
