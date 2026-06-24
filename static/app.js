const duration = document.querySelector("#duration");
const duty = document.querySelector("#duty");
const cycle = document.querySelector("#cycle");
const durationOut = document.querySelector("#durationOut");
const dutyOut = document.querySelector("#dutyOut");
const cycleOut = document.querySelector("#cycleOut");
const earthquakeBtn = document.querySelector("#earthquakeBtn");
const rumbleValue = document.querySelector("#rumbleValue");
const accelValue = document.querySelector("#accelValue");
const axisValue = document.querySelector("#axisValue");
const rumbleState = document.querySelector("#rumbleState");
const sensorStatus = document.querySelector("#sensorStatus");
const sensorKind = document.querySelector("#sensorKind");
const connectionDot = document.querySelector("#connectionDot");
const connectionText = document.querySelector("#connectionText");
const rumbleCanvas = document.querySelector("#rumbleChart");
const sensorSumCanvas = document.querySelector("#sensorSumChart");
const sensorCharts = [...document.querySelectorAll("[data-sensor-chart]")].map((canvas) => ({
  id: canvas.dataset.sensorChart,
  canvas,
  readout: document.querySelector(`[data-sensor-readout="${canvas.dataset.sensorChart}"]`),
}));

const rumbleSamples = [];
const sensorSumSamples = [];
const sensors = new Map();
let running = false;

function updateOutputs() {
  durationOut.textContent = `${Number(duration.value).toFixed(2)}s`;
  dutyOut.textContent = `${Math.round(Number(duty.value) * 100)}%`;
  cycleOut.textContent = `${Number(cycle.value).toFixed(2)}s`;
}

for (const input of [duration, duty, cycle]) {
  input.addEventListener("input", updateOutputs);
}
updateOutputs();

function pushSample(samples, value) {
  samples.push({ t: Date.now() / 1000, value });
  const cutoff = Date.now() / 1000 - 60;
  while (samples.length && samples[0].t < cutoff) {
    samples.shift();
  }
}

function drawChart(canvas, samples, color, maxValue, label) {
  const ctx = canvas.getContext("2d");
  const rect = canvas.getBoundingClientRect();
  const scale = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.floor(rect.width * scale));
  const height = Math.max(1, Math.floor(rect.height * scale));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }

  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, width, height);

  const pad = 34 * scale;
  const plotW = width - pad * 2;
  const plotH = height - pad * 1.5;
  const top = 18 * scale;
  const bottom = top + plotH;

  ctx.strokeStyle = "#e6e9ee";
  ctx.lineWidth = 1 * scale;
  ctx.fillStyle = "#64717f";
  ctx.font = `${12 * scale}px system-ui`;
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";

  for (let i = 0; i <= 4; i += 1) {
    const y = top + (plotH * i) / 4;
    ctx.beginPath();
    ctx.moveTo(pad, y);
    ctx.lineTo(width - pad, y);
    ctx.stroke();
    const tick = maxValue - (maxValue * i) / 4;
    ctx.fillText(label(tick), pad - 8 * scale, y);
  }

  ctx.strokeStyle = "#98a2ad";
  ctx.beginPath();
  ctx.moveTo(pad, bottom);
  ctx.lineTo(width - pad, bottom);
  ctx.stroke();

  if (samples.length < 2) {
    ctx.fillStyle = "#64717f";
    ctx.textAlign = "center";
    ctx.fillText("Waiting for data", width / 2, height / 2);
    return;
  }

  const now = Date.now() / 1000;
  const minT = now - 60;
  const xFor = (t) => pad + ((t - minT) / 60) * plotW;
  const yFor = (value) => top + (1 - Math.min(maxValue, Math.max(0, value)) / maxValue) * plotH;

  ctx.strokeStyle = color;
  ctx.lineWidth = 2.5 * scale;
  ctx.beginPath();
  samples.forEach((sample, index) => {
    const x = xFor(sample.t);
    const y = yFor(sample.value);
    if (index === 0) {
      ctx.moveTo(x, y);
    } else {
      ctx.lineTo(x, y);
    }
  });
  ctx.stroke();
}

function draw() {
  drawChart(rumbleCanvas, rumbleSamples, "#2563eb", 1, (v) => `${Math.round(v * 100)}%`);
  const maxSum = sensorSumSamples.reduce((max, sample) => Math.max(max, sample.value), 0);
  drawChart(sensorSumCanvas, sensorSumSamples, "#0f766e", Math.max(0.01, maxSum * 1.25), (v) => v.toFixed(3));
  for (const chart of sensorCharts) {
    const sensor = sensors.get(chart.id);
    const samples = sensor?.samples || [];
    const maxSeen = samples.reduce((max, sample) => Math.max(max, sample.value), 0);
    const maxValue = Math.max(0.01, maxSeen * 1.25);
    drawChart(chart.canvas, samples, sensor?.color || "#08916f", maxValue, (v) => v.toFixed(3));
  }
  requestAnimationFrame(draw);
}
draw();

async function startEarthquake() {
  if (running) return;
  running = true;
  earthquakeBtn.disabled = true;
  try {
    const response = await fetch("/api/rumble", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        duration: Number(duration.value),
        minDuty: 0.05,
        maxDuty: Number(duty.value),
        cycle: Number(cycle.value),
        step: 0.75,
      }),
    });
    const result = await response.json();
    if (!result.ok) throw new Error(result.error || "rumble failed");
  } catch (error) {
    rumbleState.textContent = error.message;
    running = false;
    earthquakeBtn.disabled = false;
  }
}

earthquakeBtn.addEventListener("click", startEarthquake);

const events = new EventSource("/events");

events.addEventListener("open", () => {
  connectionDot.classList.add("on");
  connectionText.textContent = "Connected";
});

events.addEventListener("error", () => {
  connectionDot.classList.remove("on");
  connectionText.textContent = "Reconnecting";
});

events.addEventListener("message", (message) => {
  const event = JSON.parse(message.data);
  if (event.type === "rumble") {
    if (event.state === "start") {
      running = true;
      rumbleState.textContent = event.dryRun ? "Running dry run" : "Running";
    }
    if (event.state === "running") {
      const value = Number(event.duty || 0);
      rumbleValue.textContent = `${Math.round(value * 100)}%`;
      pushSample(rumbleSamples, value);
    }
    if (event.state === "stop") {
      running = false;
      rumbleValue.textContent = "0%";
      rumbleState.textContent = "Idle";
      pushSample(rumbleSamples, 0);
      earthquakeBtn.disabled = false;
    }
    if (event.state === "error") {
      running = false;
      rumbleState.textContent = event.message;
      earthquakeBtn.disabled = false;
    }
  }

  if (event.type === "sensorStatus") {
    if (event.state === "connected") {
      if (event.transport === "ble") {
        sensorStatus.textContent = `Connected ${event.name}`;
      } else {
        sensorStatus.textContent = `WIT sensor on ${event.port} at ${event.baud}`;
      }
    } else if (event.state === "subscribed") {
      sensorStatus.textContent = `Streaming ${event.name}`;
    } else if (event.state === "discovered") {
      sensorStatus.textContent = `Found ${event.count} WIT BLE sensor${event.count === 1 ? "" : "s"}`;
    } else if (event.state === "scanning") {
      sensorStatus.textContent = "Scanning for WIT BLE sensors";
    } else if (event.state === "simulated") {
      sensorStatus.textContent = "Sensor stream simulated";
    } else if (event.state === "error") {
      sensorStatus.textContent = event.message;
    } else {
      sensorStatus.textContent = "Connecting to WIT sensor";
    }
  }

  if (event.type === "sensor" && (event.packet === "accel" || event.packet === "wide61")) {
    const sensorId = event.sensorId || "sensor";
    const existing = sensors.get(sensorId);
    const samples = existing?.samples || [];
    const colors = {
      "/dev/ttyUSB0": "#08916f",
      "/dev/ttyUSB1": "#b45309",
      "/dev/ttyUSB2": "#7c3aed",
    };
    const mag = Number(event.accelMag || 0);
    pushSample(samples, mag);
    sensors.set(sensorId, {
      name: event.sensorName || sensorId,
      mag,
      ax: Number(event.ax || 0),
      ay: Number(event.ay || 0),
      az: Number(event.az || 0),
      simulated: Boolean(event.simulated),
      packet: event.packet,
      samples,
      color: colors[sensorId] || existing?.color || "#08916f",
      ts: Date.now(),
    });
    const values = [...sensors.values()];
    const total = values.reduce((sum, item) => sum + item.mag, 0);
    const hasAccel = values.some((item) => item.packet === "accel" || item.simulated);
    pushSample(sensorSumSamples, total);
    accelValue.textContent = hasAccel ? `${total.toFixed(3)} g` : total.toFixed(3);
    axisValue.textContent = `${sensors.size}/3 active`;
    for (const chart of sensorCharts) {
      const sensor = sensors.get(chart.id);
      if (sensor && chart.readout) {
        chart.readout.textContent = sensor.packet === "accel" ? `${sensor.mag.toFixed(3)} g` : sensor.mag.toFixed(3);
      }
    }
    if (values.some((item) => item.simulated)) {
      sensorKind.textContent = "Acceleration magnitude, simulated";
    } else if (values.some((item) => item.packet === "wide61")) {
      sensorKind.textContent = `Total shake from ${sensors.size} floor sensor${sensors.size === 1 ? "" : "s"}`;
    } else {
      sensorKind.textContent = `Total acceleration across ${sensors.size} floor sensor${sensors.size === 1 ? "" : "s"}`;
    }
  }
});
