const modes = {
  standard_dc: {
    title: "Standard DC measurement",
    examples: "I-V curve and P-V curve.",
    body: "Sweeps the SMU DC voltage, reads PV voltage/current, applies DC safety checks, and records power.",
    vars: ["Vdc_pv", "Idc_pv", "Power", "SMU_V"],
    plots: "I-V, P-V"
  },
  frequency_sweep: {
    title: "Standard frequency sweep",
    examples: "Z-f, Z'-f, Z''-f, phase-f, C-f and Nyquist plots.",
    body: "Finds an MPP operating point or uses a manual SMU voltage, then performs an impedance frequency sweep.",
    vars: ["frequency_hz", "Z_real_ohm", "Z_imag_ohm", "Z_mag_ohm", "Z_phase_deg", "capacitance", "Vdc_pv", "Idc_pv"],
    plots: "Z magnitude, Z real, Z imaginary, phase, capacitance, Nyquist"
  },
  complete_ac: {
    title: "Complete AC measurement",
    examples: "C-V curves with a frequency range, plus impedance-related plots.",
    body: "Runs CV-style voltage points and frequency sweeps while preserving the backend filtering and outlier handling.",
    vars: ["Vdc_pv", "capacitance", "frequency_hz", "Z_real_ohm", "Z_imag_ohm", "Z_mag_ohm", "Z_phase_deg"],
    plots: "C-V, C-f, impedance plots"
  },
  live_lockin: {
    title: "Live lock-in amplifier data",
    examples: "Live magnitude/value and phase for both lock-ins.",
    body: "Starts the existing A-B differential live monitor and streams recent samples into a popup.",
    vars: ["lockin12_corrected_Vpv_Vrms", "lockin12_corrected_phase_deg", "lockin15_X_Vrms", "lockin15_phase_deg"],
    plots: "Live value and phase over time"
  }
};

let selectedMode = "standard_dc";
let currentStatus = {};
let plotConfigs = [];
let pollTimer = null;
let pendingStartPayload = null;

const settings = structuredClone(window.DEFAULT_SETTINGS);
settings.test_speed = settings.test_speed || "Medium";

const variableOptionsByMode = {
  standard_dc: [
    ["Vdc_pv", "Vdc_pv_V"],
    ["Idc_pv", "Idc_pv_A"],
    ["Power", "Pdc_pv_W"],
    ["SMU_V", "smu_voltage_V"],
    ["point_index", "point_index"]
  ],
  frequency_sweep: [
    ["frequency_hz", "f_ac_Hz"],
    ["Z_real_ohm", "Z_real_ohm"],
    ["Z_imag_ohm", "Z_imag_ohm"],
    ["Z_mag_ohm", "Z_magnitude_ohm"],
    ["Z_phase_deg", "Z_phase_deg"],
    ["capacitance", "C_uncorrected_F"],
    ["Vdc_pv", "Vdc_pv_V"],
    ["Idc_pv", "Idc_pv_A"],
    ["SMU_V", "smu_voltage_V"],
    ["Power", "Pdc_pv_W"]
  ],
  complete_ac: [
    ["Vdc_pv", "Vdc_pv_median_V"],
    ["capacitance", "C_final_median_F"],
    ["frequency_hz", "f_ac_Hz"],
    ["Z_real_ohm", "Z_real_ohm"],
    ["Z_imag_ohm", "Z_imag_ohm"],
    ["Z_mag_ohm", "Z_magnitude_ohm"],
    ["Z_phase_deg", "Z_phase_deg"],
    ["SMU_V", "smu_voltage_V"],
    ["Idc_pv", "Idc_pv_A"]
  ],
  live_lockin: [
    ["time_s", "time_s"],
    ["lock-in 12 value", "lockin12_corrected_Vpv_Vrms"],
    ["lock-in 12 phase", "lockin12_corrected_phase_deg"],
    ["lock-in 15 value", "lockin15_X_Vrms"],
    ["lock-in 15 phase", "lockin15_phase_deg"]
  ]
};

const valueAliases = {
  Vdc_pv: ["Vdc_pv_V", "Vdc_pv_median_V", "Vdc_pv_mean_V"],
  Idc_pv: ["Idc_pv_A", "Idc_pv_median_A"],
  Power: ["Pdc_pv_W"],
  SMU_V: ["smu_voltage_V", "operating_point_smu_voltage_V"],
  frequency_hz: ["f_ac_Hz"],
  Z_mag_ohm: ["Z_magnitude_ohm", "Z_mag_ohm"],
  capacitance: ["C_final_median_F", "C_uncorrected_F"]
};

function $(id) { return document.getElementById(id); }

function showScreen(id) {
  document.querySelectorAll(".screen").forEach(s => s.classList.remove("active"));
  $(id).classList.add("active");
  const step = id === "screen1" ? 1 : id === "screen2" || id === "waitingScreen" ? 2 : 3;
  document.querySelectorAll("[data-step-dot]").forEach(el => {
    el.classList.toggle("active", Number(el.dataset.stepDot) <= step);
  });
}

function setSelectedModeFromBackend(mode) {
  if (mode && window.DEFAULT_PLOTS[mode]) {
    selectedMode = mode;
    buildModes();
    updateConditionalOptions();
  }
}

function fillLiveControlInputs() {
  const controls = currentStatus.live_control || {};
  $("liveSmuVoltage").value = controls.smu_voltage_v ?? settings.manual_smu_voltage_v ?? "";
  $("liveFgFrequency").value = controls.fg_frequency_hz ?? settings.freq_start_hz ?? "";
}

function resumeRunningMeasurement() {
  startPolling();
  setSelectedModeFromBackend(currentStatus.mode);
  if (currentStatus.mode === "live_lockin") {
    fillLiveControlInputs();
    $("liveModal").showModal();
    drawLive();
    return;
  }
  buildPlotConfig();
  showScreen("screen2");
}

function buildModes() {
  const grid = $("modeGrid");
  grid.innerHTML = "";
  Object.entries(modes).forEach(([key, mode]) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "mode-card" + (key === selectedMode ? " selected" : "");
    card.innerHTML = `<strong>${mode.title}</strong><small>${mode.examples}</small><span class="icon" aria-label="Info">i</span>`;
    card.addEventListener("click", (event) => {
      if (event.target.classList.contains("icon")) {
        openInfo(key);
        return;
      }
      selectedMode = key;
      buildModes();
      updateConditionalOptions();
    });
    grid.appendChild(card);
  });
}

function fieldHtml(label, key, value, type = "number") {
  return `<label>${label}<input data-setting="${key}" type="${type}" value="${value}"></label>`;
}

function updateConditionalOptions() {
  const frequency = $("frequencyOptions");
  const ac = $("acOptions");
  frequency.classList.toggle("visible", selectedMode === "frequency_sweep");
  ac.classList.toggle("visible", selectedMode === "complete_ac");
  frequency.innerHTML = `
    <label>Operating point
      <select id="operatingPoint"><option value="mpp">Use MPP search</option><option value="manual">Manual voltage</option></select>
    </label>
    <div id="manualSmuVoltageField" class="inline-fields">
      ${fieldHtml("Manual SMU voltage [V]", "manual_smu_voltage_v", settings.manual_smu_voltage_v)}
    </div>
    ${fieldHtml("Frequency start [Hz]", "freq_start_hz", settings.freq_start_hz)}
    ${fieldHtml("Frequency stop [Hz]", "freq_stop_hz", settings.freq_stop_hz)}`;
  ac.innerHTML = `
    <label>Frequency mode
      <select id="acFrequencyMode"><option value="range">Frequency range</option><option value="single">Single frequency</option></select>
    </label>
    <div id="singleFrequencyField" class="inline-fields">
      ${fieldHtml("Single frequency [Hz]", "single_frequency_hz", settings.freq_start_hz)}
    </div>
    <div id="frequencyRangeFields" class="inline-fields">
      ${fieldHtml("Frequency start [Hz]", "freq_start_hz", settings.freq_start_hz)}
      ${fieldHtml("Frequency stop [Hz]", "freq_stop_hz", settings.freq_stop_hz)}
    </div>`;
  $("operatingPoint").addEventListener("change", updateScreenOneVisibility);
  $("acFrequencyMode").addEventListener("change", updateScreenOneVisibility);
  updateScreenOneVisibility();
}

function updateScreenOneVisibility() {
  const manualField = $("manualSmuVoltageField");
  const operatingPoint = $("operatingPoint");
  if (manualField && operatingPoint) {
    manualField.hidden = operatingPoint.value !== "manual";
  }

  const acMode = $("acFrequencyMode");
  const singleField = $("singleFrequencyField");
  const rangeFields = $("frequencyRangeFields");
  if (acMode && singleField && rangeFields) {
    singleField.hidden = acMode.value !== "single";
    rangeFields.hidden = acMode.value !== "range";
  }
}

function openInfo(key) {
  const mode = modes[key];
  $("infoTitle").textContent = mode.title;
  $("infoBody").textContent = mode.body;
  $("infoPlots").textContent = mode.plots;
  $("infoVars").textContent = mode.vars.join(", ");
  $("infoModal").showModal();
}

function buildAdvanced() {
  const keys = [
    "test_speed",
    "dmm_addr", "lockin_i_addr", "lockin_v_addr", "fg_addr", "smu_addr",
    "smu_start_v", "smu_stop_v", "smu_step_v", "cv_smu_step_v", "smu_current_limit_a",
    "max_idc_abs_a", "idc_measurement_sign", "idc_adc1_to_ampere", "vac_vpp", "fg_offset_v",
    "iac_mag_cmd", "iac_phase_cmd", "idc_adc1_cmd", "vac_mag_cmd", "vac_phase_cmd",
    "lockin_sensitivity_cmd", "settling_after_smu_s", "settling_after_freq_s",
    "max_abs_z_real_ohm", "max_outlier_retries", "simulation_mode", "output_dir"
  ];
  $("advancedGrid").innerHTML = keys.map(key => {
    if (key === "test_speed") {
      const currentSpeed = settings.test_speed || "Medium";
      return `<label>test speed<select data-advanced="test_speed">${["Medium", "Fast", "Slow"].map(speed => `<option ${speed === currentSpeed ? "selected" : ""}>${speed}</option>`).join("")}</select></label>`;
    }
    const value = settings[key];
    const type = typeof value === "boolean" ? "checkbox" : "text";
    const checked = value === true ? "checked" : "";
    return `<label>${key.replaceAll("_", " ")}<input data-advanced="${key}" type="${type}" value="${value}" ${checked}></label>`;
  }).join("");
}

function collectAdvanced() {
  document.querySelectorAll("[data-advanced]").forEach(input => {
    settings[input.dataset.advanced] = input.type === "checkbox" ? input.checked : input.value;
  });
}

function collectPayload() {
  collectAdvanced();
  const payload = { mode: selectedMode, speed: settings.test_speed || "Medium", settings };
  const freq = {};
  if ($("operatingPoint")) freq.operating_point = $("operatingPoint").value;
  document.querySelectorAll("#frequencyOptions [data-setting]").forEach(input => {
    if (input.closest("[hidden]")) return;
    freq[input.dataset.setting] = input.value;
  });
  payload.frequency = freq;
  const ac = {};
  if ($("acFrequencyMode")) ac.frequency_mode = $("acFrequencyMode").value;
  document.querySelectorAll("#acOptions [data-setting]").forEach(input => {
    if (input.closest("[hidden]")) return;
    ac[input.dataset.setting] = input.value;
  });
  payload.complete_ac = ac;
  return payload;
}

async function startMeasurement() {
  const payload = collectPayload();
  const response = await fetch("/api/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  const data = await response.json();
  if (response.status === 409) {
    pendingStartPayload = payload;
    $("runningModal").showModal();
    return;
  }
  if (!data.ok) {
    $("statusText").textContent = data.error || "Could not start measurement.";
    return;
  }
  startPolling();
  if (selectedMode === "live_lockin") {
    fillLiveControlInputs();
    $("liveModal").showModal();
  } else {
    buildPlotConfig();
    showScreen("screen2");
  }
}

async function stopCurrentAndStartPending() {
  if (!pendingStartPayload) return;
  $("runningModal").close();
  await fetch("/api/stop", { method: "POST" });
  $("statusText").textContent = "Stopping current measurement before starting the new one...";
  for (let attempt = 0; attempt < 120; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 500));
    await refreshStatus();
    if (currentStatus.status !== "running") break;
  }
  const payload = pendingStartPayload;
  pendingStartPayload = null;
  const response = await fetch("/api/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  const data = await response.json();
  if (!data.ok) {
    $("statusText").textContent = data.error || "Could not start replacement measurement.";
    return;
  }
  startPolling();
  if (payload.mode === "live_lockin") {
    fillLiveControlInputs();
    $("liveModal").showModal();
  } else {
    buildPlotConfig();
    showScreen("screen2");
  }
}

async function applyLiveControl() {
  const payload = {};
  const smu = Number($("liveSmuVoltage").value);
  const freq = Number($("liveFgFrequency").value);
  if (Number.isFinite(smu)) payload.smu_voltage_v = smu;
  if (Number.isFinite(freq)) payload.fg_frequency_hz = freq;
  const response = await fetch("/api/live/control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  const data = await response.json();
  if (!data.ok) alert("Could not apply live control settings.");
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(refreshStatus, 1000);
  refreshStatus();
}

async function refreshStatus() {
  const response = await fetch("/api/status");
  currentStatus = await response.json();
  $("statusText").textContent = `${currentStatus.status} ${currentStatus.mode ? " - " + currentStatus.mode : ""}`;
  $("resumeButton").hidden = currentStatus.status !== "running";
  $("stopButton").hidden = currentStatus.status !== "running";
  if (currentStatus.status === "failed") {
    $("statusText").textContent = currentStatus.short_error || "Measurement failed.";
  }
  if ($("liveModal").open) drawLive();
  if (currentStatus.status === "completed" && $("waitingScreen").classList.contains("active")) {
    await loadResults();
  }
}

function buildPlotConfig() {
  const count = $("plotCount");
  count.innerHTML = "";
  for (let i = 1; i <= 8; i++) count.append(new Option(String(i), String(i)));
  count.value = "2";
  renderPlotConfig();
}

function availableDefaults() {
  const mode = window.DEFAULT_PLOTS[selectedMode] ? selectedMode : (currentStatus.mode || selectedMode);
  return window.DEFAULT_PLOTS[mode] || [];
}

function allVariableOptions() {
  const vars = currentStatus.variables || {};
  const options = [];
  const seen = new Set();
  function add(label, value) {
    if (seen.has(value)) return;
    seen.add(value);
    options.push({ label, value });
  }
  const mode = variableOptionsByMode[selectedMode] ? selectedMode : (currentStatus.mode || selectedMode);
  (variableOptionsByMode[mode] || []).forEach(([label, value]) => add(label, value));
  Object.values(vars).forEach(list => list.forEach(v => add(v, v)));
  if (!options.length && modes[mode]) modes[mode].vars.forEach(v => add(v, v));
  return options;
}

function optionsHtml(options) {
  return options.map(option => `<option value="${option.value}">${option.label}</option>`).join("");
}

function updatePlotCardVisibility(card) {
  const isCustom = card.querySelector('[data-plot-field="type"]').value === "custom";
  card.querySelector(".default-fields").hidden = isCustom;
  card.querySelector(".custom-fields").hidden = !isCustom;
}

function renderPlotConfig() {
  const host = $("plotConfigs");
  const n = Number($("plotCount").value || 1);
  const defaults = availableDefaults();
  const vars = allVariableOptions();
  host.innerHTML = "";
  plotConfigs = [];
  for (let i = 0; i < n; i++) {
    const d = defaults[i % Math.max(defaults.length, 1)] || {};
    const card = document.createElement("div");
    card.className = "plot-card";
    card.innerHTML = `
      <h2>Plot ${i + 1}</h2>
      <label>Type<select data-plot-field="type"><option value="default">Default</option><option value="custom">Custom</option></select></label>
      <div class="default-fields">
        <label>Default<select data-plot-field="default">${defaults.map(x => `<option value="${x.id}">${x.label}</option>`).join("")}</select></label>
      </div>
      <div class="custom-fields">
        <label>X-axis<select data-plot-field="x">${optionsHtml(vars)}</select></label>
        <label>Y-axis<select data-plot-field="y">${optionsHtml(vars)}</select></label>
        <label>X scale<select data-plot-field="xScale"><option>linear</option><option>log</option></select></label>
        <label>Y scale<select data-plot-field="yScale"><option>linear</option><option>log</option></select></label>
      </div>`;
    card.querySelector('[data-plot-field="default"]').value = d.id || "";
    card.querySelector('[data-plot-field="type"]').addEventListener("change", () => updatePlotCardVisibility(card));
    updatePlotCardVisibility(card);
    host.appendChild(card);
  }
}

function readPlotConfigs() {
  return [...document.querySelectorAll(".plot-card")].map(card => {
    const get = field => card.querySelector(`[data-plot-field="${field}"]`).value;
    const type = get("type");
    if (type === "default") {
      return availableDefaults().find(p => p.id === get("default")) || {};
    }
    return { label: `${get("y")} vs ${get("x")}`, x: get("x"), y: get("y"), xScale: get("xScale"), yScale: get("yScale"), custom: true };
  });
}

async function waitOrResults() {
  await refreshStatus();
  plotConfigs = readPlotConfigs();
  if (currentStatus.status === "completed") {
    await loadResults();
  } else if (currentStatus.status === "failed") {
    alert(currentStatus.short_error || "Measurement failed. Full traceback is in the terminal.");
  } else {
    showScreen("waitingScreen");
  }
}

async function loadResults() {
  const response = await fetch("/api/results");
  const data = await response.json();
  showScreen("screen3");
  $("metadata").textContent = `Status: ${data.status}. Datasets: ${Object.entries(data.datasets || {}).map(([k, v]) => `${k} (${v.length})`).join(", ")}`;
  drawPlots(data.datasets || {});
}

function rowsForPlot(datasets, cfg) {
  if (cfg.dataset && datasets[cfg.dataset]) return datasets[cfg.dataset];
  return Object.values(datasets).find(rows => rows.some(row => hasValue(row, cfg.x) && hasValue(row, cfg.y))) || [];
}

function drawPlots(datasets) {
  const host = $("plots");
  host.innerHTML = "";
  plotConfigs.forEach(cfg => {
    const panel = document.createElement("div");
    panel.className = "plot-panel";
    panel.innerHTML = `<h2>${cfg.label || "Plot"}</h2><canvas width="560" height="360"></canvas>`;
    host.appendChild(panel);
    drawChart(panel.querySelector("canvas"), rowsForPlot(datasets, cfg), cfg);
  });
}

function numericPairs(rows, cfg) {
  return rows.map(row => {
    const xKey = cfg.x;
    const yKey = cfg.y;
    const x = Number(resolveValue(row, xKey));
    const y = Number(resolveValue(row, yKey));
    return { x, y };
  }).filter(p => {
    if (!Number.isFinite(p.x) || !Number.isFinite(p.y)) return false;
    if (cfg.filterBelowMin && cfg.xMin !== undefined && p.x < Number(cfg.xMin)) return false;
    if (cfg.filterBelowMin && cfg.yMin !== undefined && p.y < Number(cfg.yMin)) return false;
    return true;
  });
}

function resolveValue(row, key) {
  if (key === "neg_Z_imag_ohm") return -Number(row.Z_imag_ohm);
  if (row[key] !== undefined && row[key] !== "") return row[key];
  for (const alias of valueAliases[key] || []) {
    if (row[alias] !== undefined && row[alias] !== "") return row[alias];
  }
  return undefined;
}

function hasValue(row, key) {
  const value = resolveValue(row, key);
  return value !== undefined && value !== "" && Number.isFinite(Number(value));
}

function niceTicks(min, max, count = 5) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [];
  if (min === max) {
    const delta = Math.abs(min || 1) * 0.5;
    min -= delta;
    max += delta;
  }
  const rawStep = (max - min) / Math.max(1, count - 1);
  const power = Math.pow(10, Math.floor(Math.log10(Math.abs(rawStep) || 1)));
  const fraction = rawStep / power;
  const niceFraction = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
  const step = niceFraction * power;
  const start = Math.ceil(min / step) * step;
  const ticks = [];
  for (let value = start; value <= max + step * 0.5; value += step) ticks.push(value);
  return ticks.slice(0, count + 2);
}

function formatTick(value, isLog = false) {
  const actual = isLog ? Math.pow(10, value) : value;
  if (actual === 0) return "0";
  const abs = Math.abs(actual);
  if (abs >= 10000 || abs < 0.001) return actual.toExponential(1);
  return Number(actual.toPrecision(4)).toString();
}

function formatLiveValue(value, unit = "") {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  const abs = Math.abs(number);
  const text = abs >= 1000 || (abs > 0 && abs < 0.01)
    ? number.toExponential(3).replace("e-", "e^-").replace("e+", "e^")
    : Number(number.toPrecision(4)).toString();
  return unit ? `${text} ${unit}` : text;
}

function sizeCanvasToDisplay(canvas) {
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(320, Math.floor(rect.width));
  const height = Math.max(180, Math.floor(rect.height));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
}

function drawChart(canvas, rows, cfg) {
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const points = numericPairs(rows, cfg);
  if (!points.length) {
    ctx.fillStyle = "#647181";
    ctx.textAlign = "center";
    ctx.fillText("No compatible data for this plot.", canvas.width / 2, canvas.height / 2);
    return;
  }
  const padLeft = 70;
  const padRight = 22;
  const padTop = 22;
  const padBottom = 62;
  const xs = points.map(p => p.x);
  const ys = points.map(p => p.y);
  const logX = cfg.xScale === "log" && xs.every(v => v > 0);
  const logY = cfg.yScale === "log" && ys.every(v => v > 0);
  const tx = v => logX ? Math.log10(v) : v;
  const ty = v => logY ? Math.log10(v) : v;
  const xVals = xs.map(tx), yVals = ys.map(ty);
  let minX = Math.min(...xVals), maxX = Math.max(...xVals);
  let minY = Math.min(...yVals), maxY = Math.max(...yVals);
  if (cfg.xMin !== undefined) minX = logX ? Math.log10(Number(cfg.xMin) || Math.min(...xs.filter(v => v > 0))) : Number(cfg.xMin);
  if (cfg.yMin !== undefined) minY = logY ? Math.log10(Number(cfg.yMin) || Math.min(...ys.filter(v => v > 0))) : Number(cfg.yMin);
  if (!Number.isFinite(minX)) minX = Math.min(...xVals);
  if (!Number.isFinite(minY)) minY = Math.min(...yVals);
  if (maxX <= minX) maxX = minX + Math.abs(minX || 1);
  if (maxY <= minY) maxY = minY + Math.abs(minY || 1);
  const plotWidth = canvas.width - padLeft - padRight;
  const plotHeight = canvas.height - padTop - padBottom;
  const sx = v => padLeft + ((tx(v) - minX) / ((maxX - minX) || 1)) * plotWidth;
  const sy = v => padTop + plotHeight - ((ty(v) - minY) / ((maxY - minY) || 1)) * plotHeight;

  ctx.font = "11px Segoe UI";
  ctx.textBaseline = "middle";
  const xTicks = niceTicks(minX, maxX, 6);
  const yTicks = niceTicks(minY, maxY, 6);
  ctx.strokeStyle = "#edf0f4";
  ctx.fillStyle = "#647181";
  ctx.lineWidth = 1;
  xTicks.forEach(tick => {
    const x = padLeft + ((tick - minX) / ((maxX - minX) || 1)) * plotWidth;
    ctx.beginPath();
    ctx.moveTo(x, padTop);
    ctx.lineTo(x, padTop + plotHeight);
    ctx.stroke();
    ctx.textAlign = "center";
    ctx.fillText(formatTick(tick, logX), x, canvas.height - padBottom + 22);
  });
  yTicks.forEach(tick => {
    const y = padTop + plotHeight - ((tick - minY) / ((maxY - minY) || 1)) * plotHeight;
    ctx.beginPath();
    ctx.moveTo(padLeft, y);
    ctx.lineTo(padLeft + plotWidth, y);
    ctx.stroke();
    ctx.textAlign = "right";
    ctx.fillText(formatTick(tick, logY), padLeft - 8, y);
  });

  ctx.strokeStyle = "#d9dee6";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(padLeft, padTop);
  ctx.lineTo(padLeft, padTop + plotHeight);
  ctx.lineTo(padLeft + plotWidth, padTop + plotHeight);
  ctx.stroke();

  ctx.save();
  ctx.beginPath();
  ctx.rect(padLeft, padTop, plotWidth, plotHeight);
  ctx.clip();
  ctx.strokeStyle = "#1677ff";
  ctx.lineWidth = 2;
  ctx.beginPath();
  points.forEach((p, i) => {
    const x = sx(p.x), y = sy(p.y);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.fillStyle = "#0f8f63";
  points.forEach(p => {
    ctx.beginPath();
    ctx.arc(sx(p.x), sy(p.y), 3, 0, Math.PI * 2);
    ctx.fill();
  });
  ctx.restore();

  ctx.fillStyle = "#18202a";
  ctx.font = "12px Segoe UI";
  ctx.textAlign = "center";
  ctx.fillText(`${cfg.x || ""}${logX ? " (log)" : ""}`, padLeft + plotWidth / 2, canvas.height - 18);
  ctx.save();
  ctx.translate(16, padTop + plotHeight / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText(`${cfg.y || ""}${logY ? " (log)" : ""}`, 0, 0);
  ctx.restore();
}

function seriesPairs(rows, xKey, yKey) {
  return rows.map(row => {
    const x = Number(resolveValue(row, xKey));
    const y = Number(resolveValue(row, yKey));
    return { x, y };
  }).filter(p => Number.isFinite(p.x) && Number.isFinite(p.y));
}

function drawSeriesChart(canvas, rows, cfg) {
  sizeCanvasToDisplay(canvas);
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const series = cfg.series.map(item => ({ ...item, points: seriesPairs(rows, cfg.x, item.y) }))
    .filter(item => item.points.length);
  if (!series.length) return;

  const padLeft = 64;
  const padRight = 24;
  const padTop = 24;
  const padBottom = 58;
  const allPoints = series.flatMap(item => item.points);
  const xs = allPoints.map(p => p.x);
  const ys = allPoints.map(p => p.y);
  let minX = Math.min(...xs), maxX = Math.max(...xs);
  let minY = Math.min(...ys), maxY = Math.max(...ys);
  if (minX === maxX) maxX = minX + 1;
  if (minY === maxY) {
    const delta = Math.abs(minY || 1) * 0.5;
    minY -= delta;
    maxY += delta;
  }

  const plotWidth = canvas.width - padLeft - padRight;
  const plotHeight = canvas.height - padTop - padBottom;
  const sx = v => padLeft + ((v - minX) / ((maxX - minX) || 1)) * plotWidth;
  const sy = v => padTop + plotHeight - ((v - minY) / ((maxY - minY) || 1)) * plotHeight;
  const xTicks = niceTicks(minX, maxX, 6);
  const yTicks = niceTicks(minY, maxY, 5);

  ctx.font = "11px Segoe UI";
  ctx.textBaseline = "middle";
  ctx.strokeStyle = "#edf0f4";
  ctx.fillStyle = "#647181";
  ctx.lineWidth = 1;
  xTicks.forEach(tick => {
    const x = sx(tick);
    ctx.beginPath();
    ctx.moveTo(x, padTop);
    ctx.lineTo(x, padTop + plotHeight);
    ctx.stroke();
    ctx.textAlign = "center";
    ctx.fillText(formatTick(tick), x, canvas.height - padBottom + 22);
  });
  yTicks.forEach(tick => {
    const y = sy(tick);
    ctx.beginPath();
    ctx.moveTo(padLeft, y);
    ctx.lineTo(padLeft + plotWidth, y);
    ctx.stroke();
    ctx.textAlign = "right";
    ctx.fillText(formatTick(tick), padLeft - 8, y);
  });

  ctx.strokeStyle = "#d9dee6";
  ctx.beginPath();
  ctx.moveTo(padLeft, padTop);
  ctx.lineTo(padLeft, padTop + plotHeight);
  ctx.lineTo(padLeft + plotWidth, padTop + plotHeight);
  ctx.stroke();

  ctx.save();
  ctx.beginPath();
  ctx.rect(padLeft, padTop, plotWidth, plotHeight);
  ctx.clip();
  series.forEach(item => {
    ctx.strokeStyle = item.color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    item.points.forEach((p, i) => {
      const x = sx(p.x), y = sy(p.y);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  });
  ctx.restore();

  ctx.fillStyle = "#18202a";
  ctx.font = "12px Segoe UI";
  ctx.textAlign = "center";
  ctx.fillText(cfg.xLabel || cfg.x, padLeft + plotWidth / 2, canvas.height - 18);
  ctx.save();
  ctx.translate(16, padTop + plotHeight / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText(cfg.yLabel || "", 0, 0);
  ctx.restore();

  ctx.textAlign = "left";
  ctx.font = "11px Segoe UI";
  const legendX = padLeft + 10;
  series.forEach((item, index) => {
    const y = padTop + 16 + index * 18;
    ctx.fillStyle = item.color;
    ctx.fillRect(legendX, y - 5, 12, 4);
    ctx.fillStyle = "#18202a";
    ctx.fillText(item.label, legendX + 18, y);
  });
}

function drawLive() {
  const rows = currentStatus.live_rows || [];
  const latest = rows[rows.length - 1] || {};
  const metrics = [
    ["Vpv_dc", "Vpv_dc_V", "V"],
    ["Vpv_ac", "lockin12_corrected_Vpv_Vrms", "Vrms"],
    ["Vpv phase", "lockin12_corrected_phase_deg", "deg"],
    ["Ipv_ac", "lockin15_X_Vrms", "Vrms"],
    ["Ipv phase", "lockin15_phase_deg", "deg"]
  ];
  $("liveValues").innerHTML = metrics
    .map(([label, key, unit]) => `<div><strong>${label}</strong><span>${formatLiveValue(latest[key], unit)}</span></div>`).join("");
  $("liveLoading").hidden = rows.length > 0;
  $("liveCharts").hidden = rows.length === 0;
  if (!rows.length) return;
  drawSeriesChart($("liveValueCanvas"), rows, {
    x: "time_s",
    xLabel: "Time [s]",
    yLabel: "Value [Vrms]",
    series: [
      { y: "lockin12_corrected_Vpv_Vrms", label: "Vpv_ac", color: "#1677ff" },
      { y: "lockin15_X_Vrms", label: "Ipv_ac", color: "#0f8f63" }
    ]
  });
  drawSeriesChart($("livePhaseCanvas"), rows, {
    x: "time_s",
    xLabel: "Time [s]",
    yLabel: "Phase [deg]",
    series: [
      { y: "lockin12_corrected_phase_deg", label: "Vpv phase", color: "#8f4de8" },
      { y: "lockin15_phase_deg", label: "Ipv phase", color: "#d17b00" }
    ]
  });
}

async function uploadCsv() {
  const file = $("csvUpload").files[0];
  if (!file) return;
  const body = new FormData();
  body.append("csv_file", file);
  const response = await fetch("/api/upload", { method: "POST", body });
  const data = await response.json();
  if (!data.ok) {
    alert(data.error || "Upload failed.");
    return;
  }
  await refreshStatus();
  setSelectedModeFromBackend(data.mode || currentStatus.mode);
  buildPlotConfig();
  showScreen("screen2");
  $("advancedModal").close();
}

buildModes();
buildAdvanced();
updateConditionalOptions();
refreshStatus();

$("advancedButton").addEventListener("click", () => $("advancedModal").showModal());
$("uploadButton").addEventListener("click", uploadCsv);
$("nextFromMode").addEventListener("click", startMeasurement);
$("backToMode").addEventListener("click", () => showScreen("screen1"));
$("nextFromPlots").addEventListener("click", waitOrResults);
$("backToPlots").addEventListener("click", () => showScreen("screen2"));
$("newRun").addEventListener("click", () => showScreen("screen1"));
$("plotCount").addEventListener("change", renderPlotConfig);
$("stopButton").addEventListener("click", () => fetch("/api/stop", { method: "POST" }));
$("applyLiveControl").addEventListener("click", applyLiveControl);
$("resumeButton").addEventListener("click", resumeRunningMeasurement);
$("resumeRunningButton").addEventListener("click", () => {
  $("runningModal").close();
  resumeRunningMeasurement();
});
$("replaceRunningButton").addEventListener("click", stopCurrentAndStartPending);
$("liveCloseButton").addEventListener("click", () => $("liveModal").close());
$("liveForm").addEventListener("submit", event => {
  event.preventDefault();
  applyLiveControl();
});
