const modes = {
  standard_dc: {
    title: "Standard DC measurement",
    examples: "I-V curve and P-V curve.",
    body: "Sweeps the SMU DC voltage, reads PV voltage/current, applies DC safety checks, and records power.",
    vars: ["Vdc_pv", "Idc_pv", "Pdc_pv", "V_SMU"],
    plots: "I-V, P-V"
  },
  frequency_sweep: {
    title: "Standard frequency sweep",
    examples: "Z-f, Z'-f, Z''-f, phase-f, C-f and Nyquist plots.",
    body: "Finds an MPP operating point or uses a manual SMU voltage, then performs an impedance frequency sweep.",
    vars: ["frequency", "Z_real", "Z_imag", "Z_mag", "Phase_Z", "C", "Vac_pv", "Iac_pv", "Phase_Vac", "Phase_Iac"],
    plots: "Z magnitude, Z real, Z imaginary, phase, capacitance, Nyquist"
  },
  complete_ac: {
    title: "Complete AC measurement",
    examples: "C-V curves with a frequency range, plus impedance-related plots.",
    body: "Runs CV-style voltage points and frequency sweeps while preserving the backend filtering and outlier handling.",
    vars: ["frequency", "Z_real", "Z_imag", "Z_mag", "Phase_Z", "C", "Vac_pv", "Iac_pv", "Phase_Vac", "Phase_Iac", "Vdc_pv", "Idc_pv", "Pdc_pv"],
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
let selectedAcFrequencyMode = "range";
let pollTimer = null;
let pendingStartPayload = null;
let ledDutyTimer = null;
let customSpeedProfileTimer = null;
let pikaGameListenersReady = false;

const advancedStorageKey = `pikapv-advanced-settings:${window.APP_STARTED_AT || "current"}`;
const pikaGameBestScoreKey = "pikapv-runner-best-m";
const pikaSprite = new Image();
pikaSprite.src = window.PIKACHU_GIF_SRC || "/static/pikachu.gif";
const pikaGame = {
  running: false,
  frame: 0,
  lastTime: 0,
  width: 900,
  height: 260,
  dpr: 1,
  groundY: 210,
  distanceM: 0,
  bestM: loadPikaBestScore(),
  speedPx: 290,
  spawnIn: 0.9,
  gameOver: false,
  obstacles: [],
  player: { x: 70, y: 0, w: 58, h: 46, vy: 0, onGround: true }
};
const customSpeedProfileKeys = new Set([
  "custom_frequency_sweep_vdc_pv_step_size_v",
  "custom_frequency_sweep_frequency_points_per_decade",
  "custom_frequency_sweep_minimum_frequency_points",
  "custom_frequency_sweep_settling_after_smu_s",
  "custom_frequency_sweep_settling_after_freq_s",
  "custom_frequency_sweep_lockin_time_constant_wait_s",
  "custom_frequency_sweep_ac_samples_per_frequency",
  "custom_frequency_sweep_ac_max_impedance_spread_percent",
  "custom_frequency_sweep_ac_sample_interval_s",
  "custom_cv_vdc_pv_step_size_v",
  "custom_cv_frequency_points_per_decade",
  "custom_cv_minimum_frequency_points",
  "custom_cv_settling_after_smu_s",
  "custom_cv_settling_after_freq_s",
  "custom_cv_lockin_time_constant_wait_s",
  "custom_cv_ac_samples_per_frequency",
  "custom_cv_ac_max_impedance_spread_percent",
  "custom_cv_ac_sample_interval_s"
]);

const advancedFieldLabels = {
  custom_frequency_sweep_vdc_pv_step_size_v: "Vdc_pv Step Size [V]",
  custom_frequency_sweep_frequency_points_per_decade: "Frequency points per decade",
  custom_frequency_sweep_minimum_frequency_points: "Minimum frequency points",
  custom_frequency_sweep_settling_after_smu_s: "Settling SMU change time [s]",
  custom_frequency_sweep_settling_after_freq_s: "Settling FG change time [s]",
  custom_frequency_sweep_lockin_time_constant_wait_s: "Lockin Time wait [s]",
  custom_frequency_sweep_ac_samples_per_frequency: "AC samples per frequency",
  custom_frequency_sweep_ac_max_impedance_spread_percent: "Maximum AC impedance spread [%]",
  custom_frequency_sweep_ac_sample_interval_s: "AC sample interval [s]",
  custom_cv_vdc_pv_step_size_v: "Vdc_pv Step Size [V]",
  custom_cv_frequency_points_per_decade: "Frequency points per decade",
  custom_cv_minimum_frequency_points: "Minimum frequency points",
  custom_cv_settling_after_smu_s: "Settling SMU change time [s]",
  custom_cv_settling_after_freq_s: "Settling FG change time [s]",
  custom_cv_lockin_time_constant_wait_s: "Lockin Time wait [s]",
  custom_cv_ac_samples_per_frequency: "AC samples per frequency",
  custom_cv_ac_max_impedance_spread_percent: "Maximum AC impedance spread [%]",
  custom_cv_ac_sample_interval_s: "AC sample interval [s]",
  custom_vdc_pv_step_size_v: "Vdc_pv Step Size [V]",
  custom_frequency_points_per_decade: "Frequency points per decade",
  custom_minimum_frequency_points: "Minimum frequency points",
  settling_after_smu_s: "Settling SMU change time [s]",
  settling_after_freq_s: "Settling FG change time [s]",
  lockin_time_constant_wait_s: "Lockin Time wait [s]",
  simulation_mode: "Simulation mode",
  z_real_outlier_min_vdc_pv_v: "Z' retry minimum Vdc_pv [V]",
  stop_if_idc_negative: "End measurement when Idc becomes negative",
  dc_read_repeats: "DC samples per reading",
  dc_variation_warning_percent: "DC variation warning [%]",
  dc_vdc_variation_warning_floor_v: "Minimum Vdc warning range [V]",
  dc_idc_variation_warning_floor_a: "Minimum Idc warning range [A]"
};

function loadPersistedAdvancedSettings() {
  try {
    const raw = localStorage.getItem(advancedStorageKey);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function persistAdvancedSettings() {
  try {
    localStorage.setItem(advancedStorageKey, JSON.stringify(settings));
  } catch {
    // Local storage can be disabled; the GUI should still run normally.
  }
}

const settings = { ...structuredClone(window.DEFAULT_SETTINGS), ...loadPersistedAdvancedSettings() };
settings.test_speed = settings.test_speed || "Medium";
settings.auto_smu_range = true;

const variableOptionsByMode = {
  standard_dc: [
    ["Vdc_pv", "Vdc_pv"],
    ["Idc_pv", "Idc_pv"],
    ["Pdc_pv", "Pdc_pv"],
    ["V_SMU", "V_SMU"]
  ],
  frequency_sweep: [
    ["frequency", "frequency"],
    ["Z_real", "Z_real"],
    ["Z_imag", "Z_imag"],
    ["Z_mag", "Z_mag"],
    ["Phase_Z", "Phase_Z"],
    ["C", "C"],
    ["Vac_pv", "Vac_pv"],
    ["Iac_pv", "Iac_pv"],
    ["Phase_Vac", "Phase_Vac"],
    ["Phase_Iac", "Phase_Iac"]
  ],
  complete_ac: [
    ["frequency", "frequency"],
    ["Z_real", "Z_real"],
    ["Z_imag", "Z_imag"],
    ["Z_mag", "Z_mag"],
    ["Phase_Z", "Phase_Z"],
    ["C", "C"],
    ["Vac_pv", "Vac_pv"],
    ["Iac_pv", "Iac_pv"],
    ["Phase_Vac", "Phase_Vac"],
    ["Phase_Iac", "Phase_Iac"],
    ["Vdc_pv", "Vdc_pv"],
    ["Idc_pv", "Idc_pv"],
    ["Pdc_pv", "Pdc_pv"]
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
  Vdc_pv: ["Vdc_pv_V", "Vdc_pv_median_V", "Vdc_pv_mean_V", "operating_point_reference_Vdc_pv_V", "final_Vdc_pv"],
  Idc_pv: ["Idc_pv_A", "Idc_pv_median_A", "operating_point_reference_Idc_pv_A", "final_Idc_pv"],
  Pdc_pv: ["Pdc_pv_W", "Power", "operating_point_reference_Pdc_pv_W", "final_Pdc_pv"],
  V_SMU: ["smu_voltage_V", "SMU_V", "operating_point_smu_voltage_V", "final_V_SMU"],
  frequency: ["f_ac_Hz", "frequency_hz"],
  Z_real: ["Z_real_ohm"],
  Z_imag: ["Z_imag_ohm"],
  Z_mag: ["Z_magnitude_ohm", "Z_mag_ohm"],
  Phase_Z: ["Z_phase_deg"],
  C: ["C_final_median_F", "C_uncorrected_F", "capacitance"],
  Vac_pv: ["Vac_mag_corrected_V"],
  Iac_pv: ["Iac_mag_corrected_A"],
  Phase_Vac: ["Vac_phase_corrected_deg"],
  Phase_Iac: ["Iac_phase_corrected_deg"]
};

const acFrequencyDependentVariables = new Set([
  "Z_real",
  "Z_imag",
  "Z_mag",
  "Phase_Z",
  "C",
  "Vac_pv",
  "Iac_pv",
  "Phase_Vac",
  "Phase_Iac"
]);

function isAcFrequencyDomainVariable(variable) {
  return variable === "frequency" || acFrequencyDependentVariables.has(variable);
}

function customPlotNeedsTargetVdc(xVariable, yVariable, mode) {
  if (mode !== "complete_ac" || selectedAcFrequencyMode === "single") return false;
  return isAcFrequencyDomainVariable(xVariable) && isAcFrequencyDomainVariable(yVariable);
}

function $(id) { return document.getElementById(id); }

function showScreen(id) {
  document.querySelectorAll(".screen").forEach(s => s.classList.remove("active"));
  $(id).classList.add("active");
  const step = id === "screen1" ? 1 : id === "screen2" || id === "waitingScreen" ? 2 : 3;
  document.querySelectorAll("[data-step-dot]").forEach(el => {
    el.classList.toggle("active", Number(el.dataset.stepDot) <= step);
  });
  if (id === "waitingScreen") startPikaGame();
  else stopPikaGame();
}

function loadPikaBestScore() {
  try {
    const value = Number(localStorage.getItem(pikaGameBestScoreKey) || 0);
    return Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
  } catch {
    return 0;
  }
}

function savePikaBestScore() {
  const score = Math.floor(pikaGame.distanceM);
  if (score <= pikaGame.bestM) return;
  pikaGame.bestM = score;
  try {
    localStorage.setItem(pikaGameBestScoreKey, String(score));
  } catch {
    // The game keeps running even when localStorage is unavailable.
  }
}

function setupPikaGameListeners() {
  if (pikaGameListenersReady) return;
  pikaGameListenersReady = true;
  const canvas = $("pikaGameCanvas");
  if (canvas) {
    canvas.addEventListener("pointerdown", event => {
      event.preventDefault();
      pikaJump();
    });
  }
  const jumpButton = $("pikaJumpButton");
  if (jumpButton) jumpButton.addEventListener("click", pikaJump);
  document.addEventListener("keydown", event => {
    if (!$("waitingScreen")?.classList.contains("active")) return;
    if (![" ", "Spacebar", "ArrowUp", "w", "W"].includes(event.key) && !["Space", "KeyW"].includes(event.code)) return;
    event.preventDefault();
    pikaJump();
  });
  window.addEventListener("resize", () => {
    if ($("waitingScreen")?.classList.contains("active")) resizePikaGameCanvas();
  });
}

function startPikaGame() {
  const canvas = $("pikaGameCanvas");
  if (!canvas) return;
  setupPikaGameListeners();
  resizePikaGameCanvas();
  resetPikaGame();
  pikaGame.running = true;
  pikaGame.lastTime = performance.now();
  updatePikaScores();
  if (pikaGame.frame) cancelAnimationFrame(pikaGame.frame);
  pikaGame.frame = requestAnimationFrame(pikaGameLoop);
}

function stopPikaGame() {
  savePikaBestScore();
  pikaGame.running = false;
  if (pikaGame.frame) {
    cancelAnimationFrame(pikaGame.frame);
    pikaGame.frame = 0;
  }
}

function resizePikaGameCanvas() {
  const canvas = $("pikaGameCanvas");
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(320, rect.width || 900);
  const height = Math.max(210, rect.height || 260);
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  pikaGame.width = width;
  pikaGame.height = height;
  pikaGame.dpr = dpr;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  pikaGame.groundY = height - 44;
  if (pikaGame.player.onGround) pikaGame.player.y = pikaGame.groundY - pikaGame.player.h;
}

function resetPikaGame() {
  const player = pikaGame.player;
  pikaGame.distanceM = 0;
  pikaGame.speedPx = 290;
  pikaGame.spawnIn = 0.85;
  pikaGame.gameOver = false;
  pikaGame.obstacles = [];
  player.x = Math.min(76, pikaGame.width * 0.18);
  player.y = pikaGame.groundY - player.h;
  player.vy = 0;
  player.onGround = true;
  updatePikaScores();
}

function pikaJump() {
  if (!$("waitingScreen")?.classList.contains("active")) return;
  if (pikaGame.gameOver) {
    resetPikaGame();
    return;
  }
  const player = pikaGame.player;
  if (!player.onGround) return;
  player.vy = -760;
  player.onGround = false;
}

function pikaGameLoop(now) {
  if (!pikaGame.running) return;
  const dt = Math.min(0.034, Math.max(0, (now - pikaGame.lastTime) / 1000 || 0));
  pikaGame.lastTime = now;
  if (!pikaGame.gameOver) updatePikaGame(dt);
  drawPikaGame();
  pikaGame.frame = requestAnimationFrame(pikaGameLoop);
}

function updatePikaGame(dt) {
  const player = pikaGame.player;
  pikaGame.distanceM += dt * (pikaGame.speedPx / 28);
  pikaGame.speedPx = Math.min(560, pikaGame.speedPx + dt * 7);
  player.vy += 2200 * dt;
  player.y += player.vy * dt;
  if (player.y >= pikaGame.groundY - player.h) {
    player.y = pikaGame.groundY - player.h;
    player.vy = 0;
    player.onGround = true;
  }

  pikaGame.spawnIn -= dt;
  if (pikaGame.spawnIn <= 0) {
    spawnSolarObstacle();
    const speedFactor = Math.min(1.45, pikaGame.speedPx / 320);
    pikaGame.spawnIn = 0.95 + Math.random() * 0.75 / speedFactor;
  }

  pikaGame.obstacles.forEach(obstacle => {
    obstacle.x -= pikaGame.speedPx * dt;
  });
  pikaGame.obstacles = pikaGame.obstacles.filter(obstacle => obstacle.x + obstacle.w > -20);
  if (pikaGame.obstacles.some(obstacle => pikaCollision(player, obstacle))) {
    pikaGame.gameOver = true;
    savePikaBestScore();
  }
  updatePikaScores();
}

function spawnSolarObstacle() {
  const h = 46 + Math.random() * 30;
  const w = 34 + Math.random() * 18;
  pikaGame.obstacles.push({
    x: pikaGame.width + 18,
    y: pikaGame.groundY - h,
    w,
    h,
    voltage: Math.random() > 0.5 ? "HV" : "15 V"
  });
}

function pikaCollision(player, obstacle) {
  const p = {
    x: player.x + 9,
    y: player.y + 7,
    w: player.w - 18,
    h: player.h - 12
  };
  const o = {
    x: obstacle.x + 4,
    y: obstacle.y + 3,
    w: obstacle.w - 8,
    h: obstacle.h - 6
  };
  return p.x < o.x + o.w && p.x + p.w > o.x && p.y < o.y + o.h && p.y + p.h > o.y;
}

function updatePikaScores() {
  const score = $("pikaScore");
  const best = $("pikaBestScore");
  if (score) score.textContent = `${Math.floor(pikaGame.distanceM)} m`;
  if (best) best.textContent = `${Math.max(pikaGame.bestM, Math.floor(pikaGame.distanceM))} m`;
}

function updatePikaSpritePosition() {
  const sprite = $("pikaGameSprite");
  if (!sprite) return false;
  const p = pikaGame.player;
  const bob = p.onGround && !pikaGame.gameOver ? Math.sin(pikaGame.distanceM * 1.4) * 2 : 0;
  sprite.style.width = `${p.w}px`;
  sprite.style.height = `${p.h}px`;
  sprite.style.transform = `translate3d(${p.x}px, ${p.y + bob}px, 0)`;
  return true;
}

function drawPikaGame() {
  const canvas = $("pikaGameCanvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, pikaGame.width, pikaGame.height);
  drawPikaBackground(ctx);
  pikaGame.obstacles.forEach(obstacle => drawSolarObstacle(ctx, obstacle));
  if (!updatePikaSpritePosition()) drawPikachu(ctx);
  if (pikaGame.gameOver) drawPikaGameOver(ctx);
}

function drawPikaBackground(ctx) {
  const { width, height, groundY } = pikaGame;
  ctx.fillStyle = "#fff9df";
  ctx.fillRect(0, 0, width, height);
  ctx.fillStyle = "#eaf4ff";
  ctx.fillRect(0, 0, width, groundY);
  ctx.strokeStyle = "#d8b316";
  ctx.lineWidth = 3;
  ctx.beginPath();
  ctx.moveTo(0, groundY + 0.5);
  ctx.lineTo(width, groundY + 0.5);
  ctx.stroke();
  ctx.strokeStyle = "rgba(24, 32, 42, .08)";
  ctx.lineWidth = 1;
  const offset = -(pikaGame.distanceM * 12) % 80;
  for (let x = offset; x < width; x += 80) {
    ctx.beginPath();
    ctx.moveTo(x, groundY + 16);
    ctx.lineTo(x + 34, groundY + 16);
    ctx.stroke();
  }
}

function drawPikachu(ctx) {
  const p = pikaGame.player;
  const bob = p.onGround && !pikaGame.gameOver ? Math.sin(pikaGame.distanceM * 1.4) * 2 : 0;
  if (pikaSprite.complete && pikaSprite.naturalWidth > 0) {
    ctx.drawImage(pikaSprite, p.x, p.y + bob, p.w, p.h);
    return;
  }
  ctx.fillStyle = "#ffd735";
  roundedRect(ctx, p.x + 6, p.y + 8 + bob, p.w - 12, p.h - 10, 10);
  ctx.fill();
  ctx.fillStyle = "#2b2b2b";
  ctx.fillRect(p.x + p.w - 18, p.y + 20 + bob, 4, 4);
  ctx.fillStyle = "#e6483c";
  ctx.beginPath();
  ctx.arc(p.x + p.w - 13, p.y + 30 + bob, 4, 0, Math.PI * 2);
  ctx.fill();
}

function drawSolarObstacle(ctx, obstacle) {
  const { x, y, w, h } = obstacle;
  ctx.save();
  ctx.fillStyle = "#244f7a";
  ctx.strokeStyle = "#102a43";
  ctx.lineWidth = 2;
  roundedRect(ctx, x, y, w, h, 4);
  ctx.fill();
  ctx.stroke();

  ctx.strokeStyle = "rgba(255,255,255,.58)";
  ctx.lineWidth = 1;
  for (let i = 1; i < 3; i++) {
    const gx = x + (w / 3) * i;
    ctx.beginPath();
    ctx.moveTo(gx, y + 5);
    ctx.lineTo(gx, y + h - 5);
    ctx.stroke();
  }
  for (let i = 1; i < 4; i++) {
    const gy = y + (h / 4) * i;
    ctx.beginPath();
    ctx.moveTo(x + 5, gy);
    ctx.lineTo(x + w - 5, gy);
    ctx.stroke();
  }

  ctx.fillStyle = "#ffcf24";
  ctx.beginPath();
  ctx.moveTo(x + w * 0.58, y + 7);
  ctx.lineTo(x + w * 0.38, y + h * 0.48);
  ctx.lineTo(x + w * 0.57, y + h * 0.48);
  ctx.lineTo(x + w * 0.41, y + h - 8);
  ctx.lineTo(x + w * 0.70, y + h * 0.39);
  ctx.lineTo(x + w * 0.51, y + h * 0.39);
  ctx.closePath();
  ctx.fill();

  ctx.fillStyle = "#c93535";
  ctx.font = "bold 10px Segoe UI, Arial, sans-serif";
  ctx.textAlign = "center";
  ctx.fillText(obstacle.voltage, x + w / 2, y - 5);
  ctx.restore();
}

function drawPikaGameOver(ctx) {
  const { width, height } = pikaGame;
  ctx.fillStyle = "rgba(255, 255, 255, .78)";
  ctx.fillRect(0, 0, width, height);
  ctx.fillStyle = "#18202a";
  ctx.textAlign = "center";
  ctx.font = "700 22px Segoe UI, Arial, sans-serif";
  ctx.fillText("High voltage hit", width / 2, height / 2 - 8);
  ctx.font = "14px Segoe UI, Arial, sans-serif";
  ctx.fillText("Jump to restart", width / 2, height / 2 + 18);
}

function roundedRect(ctx, x, y, w, h, r) {
  const radius = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.lineTo(x + w - radius, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + radius);
  ctx.lineTo(x + w, y + h - radius);
  ctx.quadraticCurveTo(x + w, y + h, x + w - radius, y + h);
  ctx.lineTo(x + radius, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - radius);
  ctx.lineTo(x, y + radius);
  ctx.quadraticCurveTo(x, y, x + radius, y);
  ctx.closePath();
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
  $("liveLedDuty").value = controls.led_duty_cycle_percent ?? settings.led_duty_cycle_percent ?? 50;
}

function resumeRunningMeasurement() {
  startPolling();
  syncAcFrequencyModeFromStatus();
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
  $("acFrequencyMode").value = selectedAcFrequencyMode;
  $("operatingPoint").addEventListener("change", updateScreenOneVisibility);
  $("acFrequencyMode").addEventListener("change", () => {
    selectedAcFrequencyMode = $("acFrequencyMode").value;
    updateScreenOneVisibility();
  });
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
  const sections = [
    ["Measurement speed mode", ["test_speed", "simulation_mode", "auto_smu_step_by_speed"]],
    ["LED settings", ["led_duty_cycle_percent"]],
    ["Custom frequency sweep", ["custom_frequency_sweep_vdc_pv_step_size_v", "custom_frequency_sweep_frequency_points_per_decade", "custom_frequency_sweep_minimum_frequency_points", "custom_frequency_sweep_settling_after_smu_s", "custom_frequency_sweep_settling_after_freq_s", "custom_frequency_sweep_lockin_time_constant_wait_s", "custom_frequency_sweep_ac_samples_per_frequency", "custom_frequency_sweep_ac_max_impedance_spread_percent", "custom_frequency_sweep_ac_sample_interval_s"]],
    ["Custom CV curve", ["custom_cv_vdc_pv_step_size_v", "custom_cv_frequency_points_per_decade", "custom_cv_minimum_frequency_points", "custom_cv_settling_after_smu_s", "custom_cv_settling_after_freq_s", "custom_cv_lockin_time_constant_wait_s", "custom_cv_ac_samples_per_frequency", "custom_cv_ac_max_impedance_spread_percent", "custom_cv_ac_sample_interval_s"]],
    ["Measurement quality", ["dc_read_repeats", "dc_variation_warning_percent", "dc_vdc_variation_warning_floor_v", "dc_idc_variation_warning_floor_a"]],
    ["Safety limits", ["smu_current_limit_a", "max_smu_v", "max_vdc_pv_v", "stop_if_vdc_exceeds_max", "max_idc_abs_a", "stop_if_idc_abs_exceeds_max", "stop_if_idc_negative", "idc_adc1_to_ampere", "idc_measurement_sign"]],
    ["Lock In Amp settings", ["iac_measurement_sign", "configure_lockins", "lockin_sensitivity_cmd"]],
    ["GPIB addresses", ["dmm_addr", "lockin_i_addr", "lockin_v_addr", "fg_addr", "led_fg_addr", "smu_addr"]]
  ];
  $("advancedGrid").innerHTML = sections.map(([title, keys], index) => `
    <details class="advanced-section" ${index < 2 ? "open" : ""}>
      <summary>${title}</summary>
      <div class="advanced-section-grid">
        ${keys.map(renderAdvancedField).join("")}
      </div>
    </details>
  `).join("");
  updateAutoSmuRangeFields();
  const autoInput = document.querySelector('[data-advanced="auto_smu_range"]');
  if (autoInput) autoInput.addEventListener("change", updateAutoSmuRangeFields);
  const autoStepInput = document.querySelector('[data-advanced="auto_smu_step_by_speed"]');
  if (autoStepInput) autoStepInput.addEventListener("change", updateAutoSmuRangeFields);
  document.querySelectorAll("[data-advanced]").forEach(input => {
    input.addEventListener("change", () => {
      syncLedDutyInputs(input);
      collectAdvanced();
      syncHomepageControls();
      updateAutoSmuRangeFields();
      scheduleLedDutyUpdate(input, 0);
      scheduleCustomSpeedProfileSave(input, 0);
    });
    input.addEventListener("input", () => {
      syncLedDutyInputs(input);
      collectAdvanced();
      syncHomepageControls();
      scheduleLedDutyUpdate(input, 250);
      scheduleCustomSpeedProfileSave(input, 500);
    });
  });
}

function syncLedDutyInputs(input) {
  if (!input.matches("[data-led-duty]")) return;
  const value = Math.min(99, Math.max(1, Number(input.value || 50)));
  document.querySelectorAll("[data-led-duty]").forEach(item => {
    if (item !== input) item.value = value;
  });
}

function scheduleLedDutyUpdate(input, delayMs = 250) {
  if (!input.matches("[data-led-duty]")) return;
  if (ledDutyTimer) clearTimeout(ledDutyTimer);
  ledDutyTimer = setTimeout(applyLedDutySetting, delayMs);
}

async function applyLedDutySetting() {
  const duty = Math.min(99, Math.max(1, Number(settings.led_duty_cycle_percent || 50)));
  try {
    const response = await fetch("/api/led/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings, led_duty_cycle_percent: duty })
    });
    const data = await response.json();
    if (!data.ok) {
      $("statusText").textContent = data.error || "Could not update LED duty cycle.";
    }
  } catch {
    $("statusText").textContent = "Could not update LED duty cycle.";
  }
}

function scheduleCustomSpeedProfileSave(input, delayMs = 500) {
  const key = input.dataset.advanced;
  if (!customSpeedProfileKeys.has(key)) return;
  if (customSpeedProfileTimer) clearTimeout(customSpeedProfileTimer);
  customSpeedProfileTimer = setTimeout(saveCustomSpeedProfile, delayMs);
}

async function saveCustomSpeedProfile() {
  try {
    const response = await fetch("/api/speed-profiles/custom", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings })
    });
    const data = await response.json();
    if (!data.ok) {
      $("statusText").textContent = data.error || "Could not save custom speed profile.";
    }
  } catch {
    $("statusText").textContent = "Could not save custom speed profile.";
  }
}

function renderAdvancedField(key) {
    if (key === "test_speed") {
      const currentSpeed = settings.test_speed || "Medium";
      return `<label>test speed<select data-advanced="test_speed">${["Custom", "Fast", "Medium", "Slow"].map(speed => `<option ${speed === currentSpeed ? "selected" : ""}>${speed}</option>`).join("")}</select></label>`;
    }
    if (key === "operating_point_mode") {
      const currentMode = settings.operating_point_mode || "MPP_SEARCH";
      return `<label>operating point mode<select data-advanced="operating_point_mode">${["MPP_SEARCH", "MANUAL_SMU_VOLTAGE"].map(mode => `<option ${mode === currentMode ? "selected" : ""}>${mode}</option>`).join("")}</select></label>`;
    }
    if (key === "capacitance_unit") {
      const currentUnit = settings.capacitance_unit || "uF";
      return `<label>capacitance unit<select data-advanced="capacitance_unit">${["F", "mF", "uF", "nF"].map(unit => `<option ${unit === currentUnit ? "selected" : ""}>${unit}</option>`).join("")}</select></label>`;
    }
    if (key === "led_duty_cycle_percent") {
      const duty = Math.min(99, Math.max(1, Number(settings.led_duty_cycle_percent ?? 50)));
      return `<label class="led-duty-field">LED duty cycle [%]
        <div class="range-pair">
          <input data-advanced="led_duty_cycle_percent" data-led-duty="range" type="range" min="1" max="99" step="0.1" value="${duty}">
          <input data-advanced="led_duty_cycle_percent" data-led-duty="number" type="number" min="1" max="99" step="0.1" value="${duty}">
        </div>
      </label>`;
    }
    const value = settings[key];
    const type = typeof value === "boolean" ? "checkbox" : "text";
    const checked = value === true ? "checked" : "";
    const displayValue = isGpibAddressKey(key) ? shortGpibAddress(value) : value;
    const inputType = isGpibAddressKey(key) ? "number" : type;
    const label = advancedFieldLabels[key] || key.replaceAll("_", " ");
    const disabled = key === "auto_smu_range";
    return `<label class="${disabled ? "disabled-field" : ""}">${label}<input data-advanced="${key}" type="${inputType}" value="${displayValue}" ${checked} ${disabled ? "disabled" : ""}></label>`;
}

function collectAdvanced() {
  document.querySelectorAll("[data-advanced]").forEach(input => {
    const key = input.dataset.advanced;
    if (isGpibAddressKey(key)) {
      settings[key] = expandGpibAddress(input.value);
    } else {
      settings[key] = input.type === "checkbox" ? input.checked : input.value;
    }
  });
  if (settings.led_duty_cycle_percent !== undefined) {
    settings.led_duty_cycle_percent = Math.min(99, Math.max(1, Number(settings.led_duty_cycle_percent || 50)));
  }
  persistAdvancedSettings();
}

function isAutoSmuRangeEnabled() {
  return true;
}

function updateAutoSmuRangeFields() {
  const isAuto = isAutoSmuRangeEnabled();
  const autoStepInput = document.querySelector('[data-advanced="auto_smu_step_by_speed"]');
  if (autoStepInput) {
    autoStepInput.disabled = !isAuto;
    autoStepInput.closest("label")?.classList.toggle("disabled-field", !isAuto);
  }
  const isAutoStep = isAuto && Boolean(autoStepInput?.checked);
  ["smu_start_v", "smu_stop_v"].forEach(key => {
    const input = document.querySelector(`[data-advanced="${key}"]`);
    if (!input) return;
    input.disabled = isAuto;
    input.closest("label")?.classList.toggle("disabled-field", isAuto);
  });
  ["smu_step_v", "cv_smu_step_v"].forEach(key => {
    const input = document.querySelector(`[data-advanced="${key}"]`);
    if (!input) return;
    input.disabled = isAutoStep;
    input.closest("label")?.classList.toggle("disabled-field", isAutoStep);
  });
}

function syncHomepageControls() {
  const speed = $("homeSpeedProfile");
  if (speed && speed.value !== settings.test_speed) speed.value = settings.test_speed || "Medium";
  const duty = Math.min(99, Math.max(1, Number(settings.led_duty_cycle_percent || 50)));
  const range = $("homeLedDutyRange");
  const number = $("homeLedDutyNumber");
  if (range && Number(range.value) !== duty) range.value = duty;
  if (number && Number(number.value) !== duty) number.value = duty;
}

function syncAdvancedControl(key) {
  const inputs = document.querySelectorAll(`[data-advanced="${key}"]`);
  inputs.forEach(input => {
    if (input.type === "checkbox") input.checked = Boolean(settings[key]);
    else input.value = settings[key];
  });
}

function setupHomepageControls() {
  const speed = $("homeSpeedProfile");
  const range = $("homeLedDutyRange");
  const number = $("homeLedDutyNumber");
  syncHomepageControls();

  speed?.addEventListener("change", () => {
    settings.test_speed = speed.value;
    syncAdvancedControl("test_speed");
    persistAdvancedSettings();
  });

  [range, number].forEach(input => {
    input?.addEventListener("input", () => {
      const duty = Math.min(99, Math.max(1, Number(input.value || settings.led_duty_cycle_percent || 50)));
      settings.led_duty_cycle_percent = duty;
      syncLedDutyInputs(input);
      persistAdvancedSettings();
      scheduleLedDutyUpdate(input, 250);
    });
    input?.addEventListener("change", () => {
      const duty = Math.min(99, Math.max(1, Number(input.value || settings.led_duty_cycle_percent || 50)));
      settings.led_duty_cycle_percent = duty;
      syncLedDutyInputs(input);
      syncAdvancedControl("led_duty_cycle_percent");
      persistAdvancedSettings();
      scheduleLedDutyUpdate(input, 0);
    });
  });
}

function isGpibAddressKey(key) {
  return ["dmm_addr", "lockin_i_addr", "lockin_v_addr", "fg_addr", "led_fg_addr", "smu_addr"].includes(key);
}

function shortGpibAddress(value) {
  const match = String(value ?? "").match(/GPIB\d*::(\d+)::INSTR/i);
  return match ? match[1] : String(value ?? "");
}

function expandGpibAddress(value) {
  const text = String(value ?? "").trim();
  if (/^GPIB/i.test(text)) return text;
  return `GPIB0::${text}::INSTR`;
}

function collectPayload() {
  settings.auto_smu_range = true;
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
  if ($("acFrequencyMode")) {
    selectedAcFrequencyMode = $("acFrequencyMode").value;
    ac.frequency_mode = selectedAcFrequencyMode;
  }
  document.querySelectorAll("#acOptions [data-setting]").forEach(input => {
    if (input.closest("[hidden]")) return;
    ac[input.dataset.setting] = input.value;
  });
  payload.complete_ac = ac;
  return payload;
}

function openRunningModal() {
  $("resumeRunningButton").hidden = currentStatus.mode === "smu_calibration";
  $("runningModal").showModal();
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
    openRunningModal();
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
  const duty = Math.min(99, Math.max(1, Number($("liveLedDuty").value || settings.led_duty_cycle_percent || 50)));
  if (Number.isFinite(smu)) payload.smu_voltage_v = smu;
  if (Number.isFinite(freq)) payload.fg_frequency_hz = freq;
  if (Number.isFinite(duty)) payload.led_duty_cycle_percent = duty;
  const response = await fetch("/api/live/control", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  const data = await response.json();
  if (!data.ok) alert("Could not apply live control settings.");
  if (Number.isFinite(duty)) {
    settings.led_duty_cycle_percent = duty;
    $("liveLedDuty").value = duty;
    persistAdvancedSettings();
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
  if (!$("screen1").classList.contains("active")) syncAcFrequencyModeFromStatus();
  $("statusText").textContent = `${currentStatus.status} ${currentStatus.mode ? " - " + currentStatus.mode : ""}`;
  $("resumeButton").hidden = currentStatus.status !== "running" || currentStatus.mode === "smu_calibration";
  $("resumeRunningButton").hidden = currentStatus.mode === "smu_calibration";
  $("stopButton").hidden = currentStatus.status !== "running";
  if (currentStatus.status === "failed") {
    $("statusText").textContent = currentStatus.short_error || "Measurement failed.";
  }
  updateRunProgress();
  if ($("liveModal").open) drawLive();
  if (["completed", "failed", "stopped"].includes(currentStatus.status) && $("waitingScreen").classList.contains("active")) {
    await loadResults();
  }
}

function syncAcFrequencyModeFromStatus() {
  if (currentStatus.measurement_options?.ac_frequency_mode) {
    selectedAcFrequencyMode = currentStatus.measurement_options.ac_frequency_mode;
  }
}

function buildPlotConfig() {
  const count = $("plotCount");
  count.innerHTML = "";
  for (let i = 1; i <= 8; i++) count.append(new Option(String(i), String(i)));
  if (selectedMode === "frequency_sweep") count.value = "5";
  else if (selectedMode === "standard_dc") count.value = "2";
  else count.value = "1";
  renderPlotConfig();
}

function availableDefaults() {
  const mode = window.DEFAULT_PLOTS[selectedMode] ? selectedMode : (currentStatus.mode || selectedMode);
  const defaults = window.DEFAULT_PLOTS[mode] || [];
  if (mode === "complete_ac" && selectedAcFrequencyMode === "single") {
    return defaults
      .filter(plot => plot.id !== "cf_at_vdc")
      .map(plot => {
        if (plot.id === "cv") return { ...plot, label: "C-V (single frequency)" };
        if (plot.id === "cv_per_area") return { ...plot, label: "C-V over Area (single frequency)" };
        return plot;
      });
  }
  return defaults;
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
  if (!variableOptionsByMode[mode]) Object.values(vars).forEach(list => list.forEach(v => add(v, v)));
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
  const defaultId = card.querySelector('[data-plot-field="default"]')?.value;
  const selectedDefault = availableDefaults().find(p => p.id === defaultId);
  const targetField = card.querySelector(".target-vdc-field");
  const defaultTargetVdcInput = card.querySelector('[data-plot-field="targetVdc"]');
  const defaultAreaField = card.querySelector(".default-area-field");
  const defaultAreaInput = card.querySelector('[data-plot-field="defaultArea"]');
  const customPerArea = card.querySelector('[data-plot-field="perArea"]');
  const customAreaField = card.querySelector(".custom-area-field");
  const customAreaInput = card.querySelector('[data-plot-field="customArea"]');
  const customTargetVdcField = card.querySelector(".custom-target-vdc-field");
  const customTargetVdcInput = card.querySelector('[data-plot-field="customTargetVdc"]');
  const customTargetFrequencyField = card.querySelector(".custom-target-frequency-field");
  const customTargetFrequencyInput = card.querySelector('[data-plot-field="targetFrequency"]');
  const xVariable = card.querySelector('[data-plot-field="x"]')?.value;
  const yVariable = card.querySelector('[data-plot-field="y"]')?.value;
  const mode = window.DEFAULT_PLOTS[selectedMode] ? selectedMode : (currentStatus.mode || selectedMode);
  const needsTargetFrequency = isCustom
    && mode === "complete_ac"
    && selectedAcFrequencyMode !== "single"
    && (
      (xVariable === "Vdc_pv" && isAcFrequencyDomainVariable(yVariable))
      || (yVariable === "Vdc_pv" && isAcFrequencyDomainVariable(xVariable))
    );
  const needsTargetVdc = isCustom && customPlotNeedsTargetVdc(xVariable, yVariable, mode);
  if (targetField) targetField.hidden = isCustom || !selectedDefault?.needsTargetVdc;
  if (defaultAreaField) defaultAreaField.hidden = isCustom || !selectedDefault?.needsArea;
  if (customAreaField) customAreaField.hidden = !isCustom || !customPerArea?.checked;
  if (customTargetVdcField) customTargetVdcField.hidden = !needsTargetVdc;
  if (customTargetFrequencyField) customTargetFrequencyField.hidden = !needsTargetFrequency;
  if (defaultTargetVdcInput) defaultTargetVdcInput.required = !isCustom && Boolean(selectedDefault?.needsTargetVdc);
  if (defaultAreaInput) defaultAreaInput.required = !isCustom && Boolean(selectedDefault?.needsArea);
  if (customAreaInput) customAreaInput.required = isCustom && Boolean(customPerArea?.checked);
  if (customTargetVdcInput) customTargetVdcInput.required = needsTargetVdc;
  if (customTargetFrequencyInput) customTargetFrequencyInput.required = needsTargetFrequency;
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
        <label class="target-vdc-field">Target Vdc_pv [V]<input data-plot-field="targetVdc" type="number" step="0.001" placeholder="closest measured"></label>
        <label class="default-area-field">Solar-cell area [cm&sup2;]<input data-plot-field="defaultArea" type="number" min="0.000000001" step="any" placeholder="enter active area"></label>
      </div>
      <div class="custom-fields">
        <label>X-axis<select data-plot-field="x">${optionsHtml(vars)}</select></label>
        <label>Y-axis<select data-plot-field="y">${optionsHtml(vars)}</select></label>
        <label>X scale<select data-plot-field="xScale"><option>linear</option><option>log</option></select></label>
        <label>Y scale<select data-plot-field="yScale"><option>linear</option><option>log</option></select></label>
        <label class="custom-target-vdc-field">Target Vdc_pv [V]<input data-plot-field="customTargetVdc" type="number" step="0.001" placeholder="closest measured"></label>
        <label class="custom-target-frequency-field">Target frequency [Hz]<input data-plot-field="targetFrequency" type="number" min="0.000000001" step="any" value="${settings.freq_start_hz}" placeholder="closest measured"></label>
        <label class="plot-option-check"><input data-plot-field="perArea" type="checkbox"> Normalize Y-axis by area</label>
        <label class="custom-area-field">Solar-cell area [cm&sup2;]<input data-plot-field="customArea" type="number" min="0.000000001" step="any" placeholder="enter active area"></label>
      </div>`;
    card.querySelector('[data-plot-field="default"]').value = d.id || "";
    card.querySelector('[data-plot-field="type"]').addEventListener("change", () => updatePlotCardVisibility(card));
    card.querySelector('[data-plot-field="default"]').addEventListener("change", () => updatePlotCardVisibility(card));
    card.querySelector('[data-plot-field="perArea"]').addEventListener("change", () => updatePlotCardVisibility(card));
    card.querySelector('[data-plot-field="x"]').addEventListener("change", () => updatePlotCardVisibility(card));
    card.querySelector('[data-plot-field="y"]').addEventListener("change", () => updatePlotCardVisibility(card));
    updatePlotCardVisibility(card);
    host.appendChild(card);
  }
}

function readPlotConfigs() {
  return [...document.querySelectorAll(".plot-card")].map(card => {
    const get = field => card.querySelector(`[data-plot-field="${field}"]`).value;
    const type = get("type");
    if (type === "default") {
      const cfg = { ...(availableDefaults().find(p => p.id === get("default")) || {}) };
      const targetInput = card.querySelector('[data-plot-field="targetVdc"]');
      if (targetInput && targetInput.value !== "") cfg.targetVdc = Number(targetInput.value);
      const areaInput = card.querySelector('[data-plot-field="defaultArea"]');
      if (cfg.needsArea && areaInput?.value !== "") cfg.areaCm2 = Number(areaInput.value);
      applyNyquistPlotSign(cfg);
      return cfg;
    }
    const xSelect = card.querySelector('[data-plot-field="x"]');
    const ySelect = card.querySelector('[data-plot-field="y"]');
    const xLabel = xSelect.selectedOptions[0]?.textContent || get("x");
    const yLabel = ySelect.selectedOptions[0]?.textContent || get("y");
    const perArea = card.querySelector('[data-plot-field="perArea"]').checked;
    const areaInput = card.querySelector('[data-plot-field="customArea"]');
    const areaCm2 = areaInput?.value !== "" ? Number(areaInput.value) : undefined;
    const targetVdcInput = card.querySelector('[data-plot-field="customTargetVdc"]');
    const targetVdc = targetVdcInput?.required && targetVdcInput.value !== ""
      ? Number(targetVdcInput.value)
      : undefined;
    const targetFrequencyInput = card.querySelector('[data-plot-field="targetFrequency"]');
    const targetFrequency = targetFrequencyInput?.required && targetFrequencyInput.value !== ""
      ? Number(targetFrequencyInput.value)
      : undefined;
    return {
      label: `${yLabel}${perArea ? " / Area" : ""} over ${xLabel}`,
      x: get("x"),
      y: get("y"),
      xLabel,
      yLabel: perArea ? `${yLabel} / Area [per cm\u00b2]` : yLabel,
      xScale: get("xScale"),
      yScale: get("yScale"),
      custom: true,
      perArea,
      areaCm2,
      needsTargetVdc: Number.isFinite(targetVdc),
      targetVdc,
      needsTargetFrequency: Number.isFinite(targetFrequency),
      targetFrequency
    };
  });
}

function nyquistYAxisSign() {
  const sign = Number(settings.nyquist_y_axis_sign);
  return sign < 0 ? -1 : 1;
}

function applyNyquistPlotSign(cfg) {
  if (!cfg.nyquist) return;
  const sign = nyquistYAxisSign();
  cfg.yLabel = sign < 0 ? "-Z_imag [ohm]" : "Z_imag [ohm]";
}

async function waitOrResults() {
  const invalidAreaInput = [...document.querySelectorAll(".plot-card input:required")]
    .find(input => !input.checkValidity());
  if (invalidAreaInput) {
    invalidAreaInput.reportValidity();
    invalidAreaInput.focus();
    return;
  }
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
  try {
    const response = await fetch("/api/results");
    const data = await response.json();
    showScreen("screen3");
    $("metadata").innerHTML = renderResultsMetadata(data.datasets || {}, data.status, data.summary || {});
    drawPlots(data.datasets || {});
  } catch (error) {
    $("statusText").textContent = "Could not load results. Check the terminal output.";
    showScreen("screen3");
    $("metadata").innerHTML = `<div>Could not load results. Check the terminal output.</div>`;
    $("plots").innerHTML = "";
  }
}

function displayNumber(value, digits = 6) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "";
  return Number(number.toPrecision(digits)).toString();
}

function renderResultsMetadata(datasets, status, summary = {}) {
  const datasetText = Object.entries(datasets).map(([k, v]) => `${k} (${v.length})`).join(", ");
  let html = `<div>Status: ${status}. Datasets: ${datasetText}</div>`;
  const mode = window.DEFAULT_PLOTS[selectedMode] ? selectedMode : currentStatus.mode;
  const row = resultOperatingPointRow(mode, datasets, summary);
  if (row) {
    const values = {
      Vdc_pv: firstValue(row, ["operating_point_reference_Vdc_pv_V", "final_Vdc_pv", "mpp_search_Vdc_pv_V", "Vdc_pv", "Vdc_pv_V"]),
      Idc_pv: firstValue(row, ["operating_point_reference_Idc_pv_A", "final_Idc_pv", "mpp_search_Idc_pv_A", "Idc_pv", "Idc_pv_A", "Idc_pv_median_A"]),
      Pdc_pv: firstValue(row, ["operating_point_reference_Pdc_pv_W", "final_Pdc_pv", "mpp_search_Pdc_pv_W", "Pdc_pv", "Pdc_pv_W"]),
      V_SMU: firstValue(row, ["operating_point_smu_voltage_V", "final_V_SMU", "mpp_smu_voltage_V", "V_SMU", "smu_voltage_V", "SMU_V"])
    };
    html += `<table><thead><tr><th>MPP / operating Vdc_pv [V]</th><th>MPP / operating Idc_pv [A]</th><th>MPP / operating Pdc_pv [W]</th><th>V_SMU [V]</th></tr></thead>
      <tbody><tr><td>${displayNumber(values.Vdc_pv)}</td><td>${displayNumber(values.Idc_pv)}</td><td>${displayNumber(values.Pdc_pv)}</td><td>${displayNumber(values.V_SMU)}</td></tr></tbody></table>`;
  }
  return html;
}

function firstValue(row, keys) {
  for (const key of keys) {
    if (isUsablePlotValue(row[key])) return row[key];
  }
  return "";
}

function resultOperatingPointRow(mode, datasets, summary) {
  if (mode === "frequency_sweep") return frequencyOperatingPointRow(datasets);
  if (mode === "standard_dc") {
    if (summary.mpp_row) return summary.mpp_row;
    return maxPowerRow(datasets.iv_pv_sweep || []);
  }
  if (mode === "complete_ac") {
    return maxPowerRow([...(datasets.cv_curve || []), ...(datasets.cv_frequency_sweeps || [])]);
  }
  return null;
}

function maxPowerRow(rows) {
  const candidates = rows.filter(row => Number.isFinite(Number(resolveValue(row, "Pdc_pv"))));
  if (!candidates.length) return null;
  return candidates.reduce((best, row) => Number(resolveValue(row, "Pdc_pv")) > Number(resolveValue(best, "Pdc_pv")) ? row : best);
}

function frequencyOperatingPointRow(datasets) {
  if (datasets.frequency_sweep?.length) {
    return datasets.frequency_sweep[0];
  }
  const rows = datasets.frequency_dc || [];
  const candidates = rows.filter(row => {
    const idc = Number(resolveValue(row, "Idc_pv"));
    const pdc = Number(resolveValue(row, "Pdc_pv"));
    return Number.isFinite(idc) && Number.isFinite(pdc) && idc >= 0;
  });
  if (candidates.length) {
    return candidates.reduce((best, row) => Number(resolveValue(row, "Pdc_pv")) > Number(resolveValue(best, "Pdc_pv")) ? row : best);
  }
  return rows.length ? rows[rows.length - 1] : null;
}

function rowsForPlot(datasets, cfg) {
  let rows;
  if (cfg.dataset && datasets[cfg.dataset]) {
    rows = datasets[cfg.dataset];
  } else if (cfg.needsTargetFrequency) {
    rows = Object.values(datasets).find(items => items.some(row =>
      hasValue(row, cfg.x) && hasValue(row, cfg.y) && hasValue(row, "frequency")
    )) || [];
  } else {
    rows = Object.values(datasets).find(items => items.some(row => hasValue(row, cfg.x) && hasValue(row, cfg.y))) || [];
  }
  if (cfg.needsTargetFrequency && Number.isFinite(cfg.targetFrequency)) {
    rows = rowsAtClosestFrequencyPerVoltage(rows, cfg);
  }
  if (cfg.needsTargetVdc && Number.isFinite(cfg.targetVdc)) {
    rows = rowsAtClosestVdcSweep(rows, cfg);
  }
  return rows;
}

function medianNumber(values) {
  const sorted = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (!sorted.length) return NaN;
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

function rowsAtClosestVdcSweep(rows, cfg) {
  const groups = new Map();
  rows.forEach((row, index) => {
    const vdc = Number(resolveValue(row, "Vdc_pv"));
    if (!Number.isFinite(vdc)) return;
    const groupValue = row.sweep_index ?? row.smu_voltage_V ?? row.V_SMU ?? row.SMU_V;
    const key = groupValue !== undefined && groupValue !== "" ? String(groupValue) : String(index);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  });

  const candidates = [...groups.values()]
    .map(groupRows => ({
      rows: groupRows,
      vdc: medianNumber(groupRows.map(row => Number(resolveValue(row, "Vdc_pv"))))
    }))
    .filter(candidate => Number.isFinite(candidate.vdc));
  if (!candidates.length) return rows;

  const closest = candidates.reduce((best, candidate) =>
    Math.abs(candidate.vdc - cfg.targetVdc) < Math.abs(best.vdc - cfg.targetVdc) ? candidate : best
  );
  cfg.actualVdc = closest.vdc;
  return closest.rows;
}

function rowsAtClosestFrequencyPerVoltage(rows, cfg) {
  const groups = new Map();
  rows.forEach((row, index) => {
    if (!hasValue(row, cfg.x) || !hasValue(row, cfg.y) || !hasValue(row, "frequency")) return;
    const groupValue = row.sweep_index ?? row.smu_voltage_V ?? row.V_SMU ?? row.SMU_V ?? resolveValue(row, "Vdc_pv");
    const key = groupValue !== undefined && groupValue !== "" ? String(groupValue) : String(index);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(row);
  });

  const selectedFrequencies = [];
  const selectedRows = [];
  groups.forEach(groupRows => {
    const closestFrequency = groupRows.reduce((best, row) => {
      const frequency = Number(resolveValue(row, "frequency"));
      return Math.abs(frequency - cfg.targetFrequency) < Math.abs(best - cfg.targetFrequency) ? frequency : best;
    }, Number(resolveValue(groupRows[0], "frequency")));
    const closestRows = groupRows.filter(row =>
      Math.abs(Number(resolveValue(row, "frequency")) - closestFrequency) <= Math.max(1e-12, Math.abs(closestFrequency) * 1e-9)
    );
    const representative = { ...closestRows[0] };
    representative[cfg.x] = medianNumber(closestRows.map(row => Number(resolveValue(row, cfg.x))));
    representative[cfg.y] = medianNumber(closestRows.map(row => Number(resolveValue(row, cfg.y))));
    representative.frequency = closestFrequency;
    selectedFrequencies.push(closestFrequency);
    selectedRows.push(representative);
  });
  cfg.actualFrequency = medianNumber(selectedFrequencies);
  cfg.actualFrequencyMin = selectedFrequencies.length ? Math.min(...selectedFrequencies) : undefined;
  cfg.actualFrequencyMax = selectedFrequencies.length ? Math.max(...selectedFrequencies) : undefined;
  return selectedRows;
}

function drawPlots(datasets) {
  const host = $("plots");
  host.innerHTML = "";
  plotConfigs.forEach(cfg => {
    const panel = document.createElement("div");
    panel.className = "plot-panel";
    const rows = rowsForPlot(datasets, cfg);
    const targetSuffix = cfg.actualVdc !== undefined ? ` closest Vdc_pv=${Number(cfg.actualVdc).toPrecision(4)} V` : "";
    const frequencyVaries = Number.isFinite(cfg.actualFrequencyMin)
      && Number.isFinite(cfg.actualFrequencyMax)
      && Math.abs(cfg.actualFrequencyMax - cfg.actualFrequencyMin) > Math.max(1e-12, Math.abs(cfg.actualFrequency) * 1e-9);
    const frequencySuffix = frequencyVaries
      ? ` closest frequencies=${displayNumber(cfg.actualFrequencyMin)}-${displayNumber(cfg.actualFrequencyMax)} Hz`
      : cfg.actualFrequency !== undefined ? ` closest frequency=${displayNumber(cfg.actualFrequency)} Hz` : "";
    const areaSuffix = cfg.perArea && validPlotArea(cfg) ? ` (Area=${displayNumber(cfg.areaCm2)} cm\u00b2)` : "";
    const suffix = `${targetSuffix}${frequencySuffix}${areaSuffix}`;
    panel.innerHTML = `<h2>${cfg.label || "Plot"}${suffix}</h2><canvas width="560" height="360"></canvas>`;
    host.appendChild(panel);
    drawChart(panel.querySelector("canvas"), rows, cfg);
  });
}

function downloadAllPlots() {
  const panels = [...document.querySelectorAll("#plots .plot-panel")]
    .map(panel => ({
      title: panel.querySelector("h2")?.textContent || "Plot",
      canvas: panel.querySelector("canvas")
    }))
    .filter(item => item.canvas);
  if (!panels.length) {
    alert("No plots are available to download.");
    return;
  }

  panels.forEach((item, index) => {
    const safeTitle = item.title
      .normalize("NFKD")
      .replace(/[^\w.-]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .slice(0, 100) || `plot_${index + 1}`;
    const filename = `PikaPV_${safeTitle}.png`;
    item.canvas.toBlob(blob => {
      if (!blob) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }, "image/png");
  });
}

function validPlotArea(cfg) {
  return Number.isFinite(Number(cfg.areaCm2)) && Number(cfg.areaCm2) > 0;
}

function numericPairs(rows, cfg) {
  let points = rows.map(row => {
    const xKey = cfg.x;
    const yKey = cfg.y;
    const x = Number(resolveValue(row, xKey));
    let y = Number(resolveValue(row, yKey));
    if (cfg.nyquist) y *= nyquistYAxisSign();
    if (cfg.perArea) y /= Number(cfg.areaCm2);
    return { x, y };
  }).filter(p => Number.isFinite(p.x) && Number.isFinite(p.y));
  points = extendPlotStartToXMinimum(points, cfg);
  if (!cfg.filterBelowMin || points.length < 2) {
    return points.filter(p => pointMeetsPlotMinimums(p, cfg));
  }

  const clipped = [];
  for (let index = 1; index < points.length; index++) {
    const segment = clipSegmentToPlotMinimums(points[index - 1], points[index], cfg);
    if (!segment) continue;
    appendUniquePlotPoint(clipped, segment[0]);
    appendUniquePlotPoint(clipped, segment[1]);
  }
  return clipped;
}

function extendPlotStartToXMinimum(points, cfg) {
  if (!cfg.extendToXMin || cfg.xMin === undefined || points.length < 2) return points;
  const xMinimum = Number(cfg.xMin);
  const first = points[0];
  const second = points[1];
  if (!Number.isFinite(xMinimum) || first.x <= xMinimum + 1e-12 || second.x <= first.x + 1e-12) {
    return points;
  }

  const configuredY = Number(cfg.yAtXMin);
  let yAtMinimum;
  if (cfg.yAtXMin !== undefined && Number.isFinite(configuredY)) {
    yAtMinimum = configuredY;
  } else {
    const slope = (second.y - first.y) / (second.x - first.x);
    yAtMinimum = first.y + slope * (xMinimum - first.x);
  }
  if (cfg.yMin !== undefined && Number.isFinite(Number(cfg.yMin))) {
    yAtMinimum = Math.max(Number(cfg.yMin), yAtMinimum);
  }
  return [{ x: xMinimum, y: yAtMinimum }, ...points];
}

function pointMeetsPlotMinimums(point, cfg) {
  if (cfg.filterBelowMin && cfg.xMin !== undefined && point.x < Number(cfg.xMin)) return false;
  if (cfg.filterBelowMin && cfg.yMin !== undefined && point.y < Number(cfg.yMin)) return false;
  return true;
}

function clipSegmentToPlotMinimums(start, end, cfg) {
  let tStart = 0;
  let tEnd = 1;
  for (const [value, delta, minimum] of [
    [start.x, end.x - start.x, cfg.xMin],
    [start.y, end.y - start.y, cfg.yMin]
  ]) {
    if (minimum === undefined) continue;
    const min = Number(minimum);
    if (!Number.isFinite(min)) continue;
    if (Math.abs(delta) <= 1e-15) {
      if (value < min) return null;
      continue;
    }
    const crossing = (min - value) / delta;
    if (delta > 0) tStart = Math.max(tStart, crossing);
    else tEnd = Math.min(tEnd, crossing);
  }
  if (tStart > tEnd || tEnd < 0 || tStart > 1) return null;
  const firstT = Math.max(0, tStart);
  const lastT = Math.min(1, tEnd);
  return [
    interpolatePlotPoint(start, end, firstT),
    interpolatePlotPoint(start, end, lastT)
  ];
}

function interpolatePlotPoint(start, end, t) {
  return {
    x: start.x + (end.x - start.x) * t,
    y: start.y + (end.y - start.y) * t
  };
}

function appendUniquePlotPoint(points, point) {
  const last = points[points.length - 1];
  if (last && Math.abs(last.x - point.x) <= 1e-12 && Math.abs(last.y - point.y) <= 1e-12) return;
  points.push(point);
}

function isUsablePlotValue(value) {
  if (value === undefined || value === null || value === "") return false;
  return Number.isFinite(Number(value));
}

function resolveValue(row, key) {
  if (isUsablePlotValue(row[key])) return row[key];
  for (const alias of valueAliases[key] || []) {
    if (isUsablePlotValue(row[alias])) return row[alias];
  }
  return undefined;
}

function hasValue(row, key) {
  return isUsablePlotValue(resolveValue(row, key));
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

function formatLogTick(actual) {
  const abs = Math.abs(actual);
  if (abs >= 1e9) return `${Number((actual / 1e9).toPrecision(3))}G`;
  if (abs >= 1e6) return `${Number((actual / 1e6).toPrecision(3))}M`;
  if (abs >= 1e3) return `${Number((actual / 1e3).toPrecision(3))}k`;
  if (abs >= 0.001) return Number(actual.toPrecision(4)).toString();
  return actual.toExponential(0);
}

function logarithmicTicks(minLog, maxLog, pixelSpan) {
  if (!Number.isFinite(minLog) || !Number.isFinite(maxLog) || maxLog <= minLog) return [];
  const span = maxLog - minLog;
  const pixelsPerDecade = pixelSpan / span;
  const minorMultipliers = pixelsPerDecade >= 95 ? [2, 3, 4, 5, 6, 7, 8, 9] : [2, 5];
  const narrowMajorMultipliers = span < 0.45
    ? new Set([1, 2, 3, 4, 5, 6, 7, 8, 9])
    : span < 1 ? new Set([1, 2, 5]) : new Set([1]);
  const majorEvery = span > 7 ? Math.ceil(span / 7) : 1;
  const ticks = [];
  const firstExponent = Math.floor(minLog) - 1;
  const lastExponent = Math.ceil(maxLog) + 1;

  for (let exponent = firstExponent; exponent <= lastExponent; exponent++) {
    const multipliers = [1, ...minorMultipliers];
    multipliers.forEach(multiplier => {
      const value = exponent + Math.log10(multiplier);
      if (value < minLog - 1e-10 || value > maxLog + 1e-10) return;
      const isDecade = multiplier === 1;
      const labelTick = narrowMajorMultipliers.has(multiplier)
        && (!isDecade || Math.abs(exponent % majorEvery) === 0);
      ticks.push({
        value,
        major: isDecade || labelTick,
        label: labelTick ? formatLogTick(multiplier * Math.pow(10, exponent)) : ""
      });
    });
  }
  return ticks.sort((a, b) => a.value - b.value);
}

function linearGridTicks(min, max, count, isLog = false) {
  return niceTicks(min, max, count).map(value => ({
    value,
    major: true,
    label: formatTick(value, isLog)
  }));
}

function axisRange(values, forcedMin, isLog = false) {
  let dataMin = Math.min(...values);
  let dataMax = Math.max(...values);
  let min = dataMin;
  let max = dataMax;
  if (forcedMin !== undefined) {
    const forced = Number(forcedMin);
    min = isLog ? Math.log10(forced || Math.min(...values.filter(v => v > 0))) : forced;
  }
  if (!Number.isFinite(min)) min = dataMin;
  if (!Number.isFinite(max)) max = dataMax;
  const span = max - dataMin;
  if (forcedMin !== undefined && !isLog && min === 0 && dataMin > 0 && span > 0 && dataMin / Math.max(max, 1e-30) > 0.55) {
    min = Math.max(0, dataMin - span * 0.12);
    return { min, max: max + span * 0.08, axisBreakAtZero: true };
  }
  const range = max - min;
  if (range > 0) max += range * 0.06;
  if (max <= min) max = min + Math.abs(min || 1);
  return { min, max, axisBreakAtZero: false };
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

function formatDuration(seconds) {
  const number = Number(seconds);
  if (!Number.isFinite(number) || number < 0) return "--";
  const total = Math.round(number);
  const minutes = Math.floor(total / 60);
  const secs = total % 60;
  if (minutes >= 60) {
    const hours = Math.floor(minutes / 60);
    const remMin = minutes % 60;
    return `${hours}h ${String(remMin).padStart(2, "0")}m`;
  }
  return `${minutes}m ${String(secs).padStart(2, "0")}s`;
}

function updateRunProgress() {
  const progress = currentStatus.progress || {};
  const isRunning = currentStatus.status === "running" && currentStatus.mode !== "live_lockin";
  $("runProgress").hidden = !isRunning;
  if (!isRunning) return;
  const progressPercent = Number(progress.percent);
  const percent = Number.isFinite(progressPercent)
    ? Math.max(0, Math.min(100, progressPercent))
    : Math.min(96, ((Date.now() / 1000) % 30) / 30 * 100);
  $("runProgressFill").style.width = `${percent}%`;
  $("pikachuRunner").style.left = `${percent}%`;
  $("runProgressTitle").textContent = progress.label
    ? `${progress.label} progress`
    : "Measurement progress";
  if (progress.hide_time || progress.indeterminate) {
    $("runProgressText").textContent = progress.message || "Running...";
  } else {
    const cacheNote = Number(progress.auto_smu_step_missing_points || 0) > 0
      ? " | includes auto SMU search"
      : "";
    $("runProgressText").textContent =
      `${percent.toFixed(0)}% | elapsed ${formatDuration(progress.elapsed_s)} | remaining ${formatDuration(progress.remaining_s)}${cacheNote}`;
  }
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
  if (cfg.perArea && !validPlotArea(cfg)) {
    ctx.fillStyle = "#647181";
    ctx.textAlign = "center";
    ctx.fillText("Enter a positive solar-cell area to generate this plot.", canvas.width / 2, canvas.height / 2);
    return;
  }
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
  const xRange = axisRange(xVals, cfg.xMin, logX);
  const yRange = axisRange(yVals, cfg.yMin, logY);
  let minX = xRange.min, maxX = xRange.max;
  let minY = yRange.min, maxY = yRange.max;
  const plotWidth = canvas.width - padLeft - padRight;
  const plotHeight = canvas.height - padTop - padBottom;
  const sx = v => padLeft + ((tx(v) - minX) / ((maxX - minX) || 1)) * plotWidth;
  const sy = v => padTop + plotHeight - ((ty(v) - minY) / ((maxY - minY) || 1)) * plotHeight;

  ctx.font = "11px Segoe UI";
  ctx.textBaseline = "middle";
  let xTicks = logX ? logarithmicTicks(minX, maxX, plotWidth) : linearGridTicks(minX, maxX, 6);
  if (xRange.axisBreakAtZero) {
    xTicks = [{ value: 0, major: true, label: "0" }, ...xTicks.filter(tick => tick.value > minX + 1e-12)];
  }
  const yTicks = logY ? logarithmicTicks(minY, maxY, plotHeight) : linearGridTicks(minY, maxY, 6);
  xTicks.forEach(tick => {
    if (xRange.axisBreakAtZero && tick.value === 0) return;
    const x = padLeft + ((tick.value - minX) / ((maxX - minX) || 1)) * plotWidth;
    ctx.strokeStyle = tick.major ? "#e1e6ed" : "#f1f3f6";
    ctx.lineWidth = tick.major ? 1 : 0.75;
    ctx.beginPath();
    ctx.moveTo(x, padTop);
    ctx.lineTo(x, padTop + plotHeight);
    ctx.stroke();
    if (tick.label) {
      ctx.fillStyle = "#647181";
      ctx.textAlign = x < padLeft + 16 ? "left" : x > padLeft + plotWidth - 16 ? "right" : "center";
      ctx.fillText(tick.label, x, canvas.height - padBottom + 22);
    }
  });
  yTicks.forEach(tick => {
    const y = padTop + plotHeight - ((tick.value - minY) / ((maxY - minY) || 1)) * plotHeight;
    ctx.strokeStyle = tick.major ? "#e1e6ed" : "#f1f3f6";
    ctx.lineWidth = tick.major ? 1 : 0.75;
    ctx.beginPath();
    ctx.moveTo(padLeft, y);
    ctx.lineTo(padLeft + plotWidth, y);
    ctx.stroke();
    if (tick.label) {
      ctx.fillStyle = "#647181";
      ctx.textAlign = "right";
      ctx.fillText(tick.label, padLeft - 8, y);
    }
  });

  ctx.strokeStyle = "#d9dee6";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(padLeft, padTop);
  ctx.lineTo(padLeft, padTop + plotHeight);
  ctx.lineTo(padLeft + plotWidth, padTop + plotHeight);
  ctx.stroke();
  if (xRange.axisBreakAtZero) {
    ctx.strokeStyle = "#9aa5b1";
    ctx.beginPath();
    ctx.moveTo(padLeft + 10, padTop + plotHeight - 8);
    ctx.lineTo(padLeft + 18, padTop + plotHeight + 4);
    ctx.moveTo(padLeft + 18, padTop + plotHeight - 8);
    ctx.lineTo(padLeft + 26, padTop + plotHeight + 4);
    ctx.stroke();
    ctx.fillStyle = "#647181";
    ctx.textAlign = "center";
    ctx.fillText("0", padLeft, canvas.height - padBottom + 22);
  }

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
  ctx.fillText(`${cfg.xLabel || cfg.x || ""}${logX ? " (log scale)" : ""}`, padLeft + plotWidth / 2, canvas.height - 18);
  ctx.save();
  ctx.translate(16, padTop + plotHeight / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillText(`${cfg.yLabel || cfg.y || ""}${logY ? " (log scale)" : ""}`, 0, 0);
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
    ["Ipv phase", "lockin15_phase_deg", "deg"],
    ["C_live", "live_capacitance_F", "F"]
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
  if (data.frequency_mode) selectedAcFrequencyMode = data.frequency_mode;
  setSelectedModeFromBackend(data.mode || currentStatus.mode);
  buildPlotConfig();
  showScreen("screen2");
  $("advancedModal").close();
}

buildModes();
buildAdvanced();
setupHomepageControls();
updateConditionalOptions();
refreshStatus();

$("advancedButton").addEventListener("click", () => $("advancedModal").showModal());
$("uploadButton").addEventListener("click", uploadCsv);
$("nextFromMode").addEventListener("click", startMeasurement);
$("backToMode").addEventListener("click", () => showScreen("screen1"));
$("nextFromPlots").addEventListener("click", waitOrResults);
$("backToPlots").addEventListener("click", () => showScreen("screen2"));
$("downloadAllPlots").addEventListener("click", downloadAllPlots);
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
