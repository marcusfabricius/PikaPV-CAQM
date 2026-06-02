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

const settings = structuredClone(window.DEFAULT_SETTINGS);
settings.test_speed = "Medium";

function $(id) { return document.getElementById(id); }

function showScreen(id) {
  document.querySelectorAll(".screen").forEach(s => s.classList.remove("active"));
  $(id).classList.add("active");
  const step = id === "screen1" ? 1 : id === "screen2" || id === "waitingScreen" ? 2 : 3;
  document.querySelectorAll("[data-step-dot]").forEach(el => {
    el.classList.toggle("active", Number(el.dataset.stepDot) <= step);
  });
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
    ${fieldHtml("Manual SMU voltage [V]", "manual_smu_voltage_v", settings.manual_smu_voltage_v)}
    ${fieldHtml("Frequency start [Hz]", "freq_start_hz", settings.freq_start_hz)}
    ${fieldHtml("Frequency stop [Hz]", "freq_stop_hz", settings.freq_stop_hz)}
    <label>Operating point
      <select id="operatingPoint"><option value="mpp">Use MPP search</option><option value="manual">Manual voltage</option></select>
    </label>`;
  ac.innerHTML = `
    <label>Frequency mode
      <select id="acFrequencyMode"><option value="range">Frequency range</option><option value="single">Single frequency</option></select>
    </label>
    ${fieldHtml("Single frequency [Hz]", "single_frequency_hz", settings.freq_start_hz)}
    ${fieldHtml("Frequency start [Hz]", "freq_start_hz", settings.freq_start_hz)}
    ${fieldHtml("Frequency stop [Hz]", "freq_stop_hz", settings.freq_stop_hz)}
    ${fieldHtml("CV SMU step [V]", "cv_smu_step_v", settings.cv_smu_step_v)}`;
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
      return `<label>test speed<select data-advanced="test_speed"><option>Medium</option><option>Fast</option><option>Slow</option></select></label>`;
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
  document.querySelectorAll("#frequencyOptions [data-setting]").forEach(input => freq[input.dataset.setting] = input.value);
  if ($("operatingPoint")) freq.operating_point = $("operatingPoint").value;
  payload.frequency = freq;
  const ac = {};
  document.querySelectorAll("#acOptions [data-setting]").forEach(input => ac[input.dataset.setting] = input.value);
  if ($("acFrequencyMode")) ac.frequency_mode = $("acFrequencyMode").value;
  payload.complete_ac = ac;
  return payload;
}

async function startMeasurement() {
  const response = await fetch("/api/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(collectPayload())
  });
  const data = await response.json();
  if (!data.ok) {
    alert(data.error || "Could not start measurement.");
    return;
  }
  startPolling();
  if (selectedMode === "live_lockin") {
    $("liveModal").showModal();
  } else {
    buildPlotConfig();
    showScreen("screen2");
  }
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
  return window.DEFAULT_PLOTS[selectedMode] || [];
}

function allVariables() {
  const vars = currentStatus.variables || {};
  const merged = [];
  Object.values(vars).forEach(list => list.forEach(v => { if (!merged.includes(v)) merged.push(v); }));
  if (!merged.length) modes[selectedMode].vars.forEach(v => merged.push(v));
  return merged;
}

function renderPlotConfig() {
  const host = $("plotConfigs");
  const n = Number($("plotCount").value || 1);
  const defaults = availableDefaults();
  const vars = allVariables();
  host.innerHTML = "";
  plotConfigs = [];
  for (let i = 0; i < n; i++) {
    const d = defaults[i % Math.max(defaults.length, 1)] || {};
    const card = document.createElement("div");
    card.className = "plot-card";
    card.innerHTML = `
      <h2>Plot ${i + 1}</h2>
      <label>Type<select data-plot-field="type"><option value="default">Default</option><option value="custom">Custom</option></select></label>
      <label>Default<select data-plot-field="default">${defaults.map(x => `<option value="${x.id}">${x.label}</option>`).join("")}</select></label>
      <div class="custom-fields">
        <label>X-axis<select data-plot-field="x">${vars.map(v => `<option>${v}</option>`).join("")}</select></label>
        <label>Y-axis<select data-plot-field="y">${vars.map(v => `<option>${v}</option>`).join("")}</select></label>
        <label>X scale<select data-plot-field="xScale"><option>linear</option><option>log</option></select></label>
        <label>Y scale<select data-plot-field="yScale"><option>linear</option><option>log</option></select></label>
      </div>`;
    card.querySelector('[data-plot-field="default"]').value = d.id || "";
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
  return Object.values(datasets).find(rows => rows.some(row => row[cfg.x] !== undefined && row[cfg.y] !== undefined)) || [];
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

function numericPairs(rows, xKey, yKey) {
  return rows.map(row => {
    const x = xKey === "neg_Z_imag_ohm" ? -Number(row.Z_imag_ohm) : Number(row[xKey]);
    const y = yKey === "neg_Z_imag_ohm" ? -Number(row.Z_imag_ohm) : Number(row[yKey]);
    return { x, y };
  }).filter(p => Number.isFinite(p.x) && Number.isFinite(p.y));
}

function drawChart(canvas, rows, cfg) {
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const points = numericPairs(rows, cfg.x, cfg.y);
  if (!points.length) {
    ctx.fillStyle = "#647181";
    ctx.textAlign = "center";
    ctx.fillText("No compatible data for this plot.", canvas.width / 2, canvas.height / 2);
    return;
  }
  const pad = 54;
  const xs = points.map(p => p.x);
  const ys = points.map(p => p.y);
  const logX = cfg.xScale === "log" && xs.every(v => v > 0);
  const logY = cfg.yScale === "log" && ys.every(v => v > 0);
  const tx = v => logX ? Math.log10(v) : v;
  const ty = v => logY ? Math.log10(v) : v;
  const xVals = xs.map(tx), yVals = ys.map(ty);
  const minX = Math.min(...xVals), maxX = Math.max(...xVals);
  const minY = Math.min(...yVals), maxY = Math.max(...yVals);
  const sx = v => pad + ((tx(v) - minX) / ((maxX - minX) || 1)) * (canvas.width - pad * 1.4);
  const sy = v => canvas.height - pad - ((ty(v) - minY) / ((maxY - minY) || 1)) * (canvas.height - pad * 1.5);
  ctx.strokeStyle = "#d9dee6";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad, 20);
  ctx.lineTo(pad, canvas.height - pad);
  ctx.lineTo(canvas.width - 22, canvas.height - pad);
  ctx.stroke();
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
  ctx.fillStyle = "#18202a";
  ctx.font = "12px Segoe UI";
  ctx.fillText(cfg.x || "", pad, canvas.height - 18);
  ctx.save();
  ctx.translate(16, canvas.height / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText(cfg.y || "", 0, 0);
  ctx.restore();
}

function drawLive() {
  const rows = currentStatus.live_rows || [];
  const latest = rows[rows.length - 1] || {};
  $("liveValues").innerHTML = ["lockin12_corrected_Vpv_Vrms", "lockin12_corrected_phase_deg", "lockin15_X_Vrms", "lockin15_phase_deg"]
    .map(k => `<div><strong>${k}</strong><br>${latest[k] ?? ""}</div>`).join("");
  drawChart($("liveCanvas"), rows, { x: "time_s", y: "lockin12_corrected_Vpv_Vrms", label: "Live lock-in" });
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
  buildPlotConfig();
  showScreen("screen2");
  $("advancedModal").close();
}

buildModes();
buildAdvanced();
updateConditionalOptions();

$("advancedButton").addEventListener("click", () => $("advancedModal").showModal());
$("uploadButton").addEventListener("click", uploadCsv);
$("nextFromMode").addEventListener("click", startMeasurement);
$("backToMode").addEventListener("click", () => showScreen("screen1"));
$("nextFromPlots").addEventListener("click", waitOrResults);
$("backToPlots").addEventListener("click", () => showScreen("screen2"));
$("newRun").addEventListener("click", () => showScreen("screen1"));
$("plotCount").addEventListener("change", renderPlotConfig);
$("stopButton").addEventListener("click", () => fetch("/api/stop", { method: "POST" }));
