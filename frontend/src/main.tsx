import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { Activity, AlertTriangle, CircleStop, Play, RadioTower, Waves } from "lucide-react";
import "./styles.css";

type DemoState = {
  type: "demo";
  state: "idle" | "armed" | "waiting_random_delay" | "running" | "complete" | "stopped" | "error";
  duration: number;
  delay: number;
  delayRemaining?: number;
  elapsed: number;
  duty: number;
  dryRun: boolean;
  message?: string;
};

type SensorEvent = {
  type: "sensor";
  sensorId: string;
  sensorName: string;
  packet: "accel" | "wide61" | string;
  accelMag: number;
  ax?: number;
  ay?: number;
  az?: number;
  tempC?: number;
  simulated?: boolean;
  ts: number;
};

type SensorStatusEvent = {
  type: "sensorStatus";
  state: string;
  port?: string;
  name?: string;
  transport?: string;
  baud?: number;
  message?: string;
};

type ClassificationEvent = {
  type: "classification";
  label: "No motion" | "Non-Earthquake motion detected" | "Earthquake detected";
  confidence: number;
  activeSensors: number;
  energizedSensors: number;
  sustainedSensors?: number;
  totalShake: number;
};

type EventPayload = DemoState | SensorEvent | SensorStatusEvent | ClassificationEvent;

type Sample = { t: number; value: number };
type SensorState = {
  id: string;
  name: string;
  label: string;
  connected: boolean;
  value: number;
  tempC?: number;
  packet: string;
  samples: Sample[];
  lastSeen: number;
};

const floorLabels: Record<string, string> = {
  "/dev/ttyUSB0": "Top Floor",
  "/dev/ttyUSB1": "Middle Floor",
  "/dev/ttyUSB2": "Bottom Floor",
};

const floorOrder = ["/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyUSB2"];

const initialDemo: DemoState = {
  type: "demo",
  state: "idle",
  duration: 20,
  delay: 0,
  elapsed: 0,
  duty: 0,
  dryRun: false,
};

function pushSample(samples: Sample[], value: number, t = Date.now() / 1000) {
  const next = [...samples, { t, value }];
  const cutoff = t - 60;
  while (next.length && next[0].t < cutoff) next.shift();
  return next;
}

function statusText(demo: DemoState) {
  if (demo.state === "waiting_random_delay") return `Random start in ${Math.ceil(demo.delayRemaining || 0)}s`;
  if (demo.state === "running") return `Running ${Math.min(100, Math.round((demo.elapsed / demo.duration) * 100))}%`;
  if (demo.state === "complete") return "Complete";
  if (demo.state === "stopped") return "Stopped";
  if (demo.state === "error") return demo.message || "Error";
  return demo.state === "armed" ? "Armed" : "Idle";
}

function Chart({ samples, color, max, label }: { samples: Sample[]; color: string; max?: number; label?: string }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const rect = canvas.getBoundingClientRect();
    const scale = window.devicePixelRatio || 1;
    const width = Math.max(1, Math.floor(rect.width * scale));
    const height = Math.max(1, Math.floor(rect.height * scale));
    canvas.width = width;
    canvas.height = height;
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, width, height);

    const pad = 30 * scale;
    const top = 16 * scale;
    const plotW = width - pad * 2;
    const plotH = height - top - pad;
    const bottom = top + plotH;
    const maxSeen = samples.reduce((acc, sample) => Math.max(acc, sample.value), 0);
    const maxValue = Math.max(0.01, max || maxSeen * 1.2);

    ctx.strokeStyle = "#e4e7ec";
    ctx.lineWidth = scale;
    ctx.fillStyle = "#667085";
    ctx.font = `${11 * scale}px system-ui`;
    ctx.textAlign = "right";
    for (let i = 0; i <= 3; i += 1) {
      const y = top + (plotH * i) / 3;
      ctx.beginPath();
      ctx.moveTo(pad, y);
      ctx.lineTo(width - pad, y);
      ctx.stroke();
      ctx.fillText((maxValue - (maxValue * i) / 3).toFixed(maxValue > 0.1 ? 2 : 3), pad - 7 * scale, y + 4 * scale);
    }

    if (samples.length < 2) {
      ctx.fillStyle = "#98a2b3";
      ctx.textAlign = "center";
      ctx.fillText(label || "Waiting for data", width / 2, height / 2);
      return;
    }

    const now = Date.now() / 1000;
    const minT = now - 60;
    const xFor = (t: number) => pad + ((t - minT) / 60) * plotW;
    const yFor = (value: number) => top + (1 - Math.min(maxValue, Math.max(0, value)) / maxValue) * plotH;
    ctx.strokeStyle = color;
    ctx.lineWidth = 2.5 * scale;
    ctx.beginPath();
    samples.forEach((sample, index) => {
      const x = xFor(sample.t);
      const y = yFor(sample.value);
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.strokeStyle = "#98a2b3";
    ctx.beginPath();
    ctx.moveTo(pad, bottom);
    ctx.lineTo(width - pad, bottom);
    ctx.stroke();
  }, [samples, color, max, label]);

  return <canvas className="chart" ref={canvasRef} />;
}

function App() {
  const [connected, setConnected] = useState(false);
  const [demo, setDemo] = useState<DemoState>(initialDemo);
  const [classification, setClassification] = useState<ClassificationEvent | null>(null);
  const [sensorStatus, setSensorStatus] = useState("Waiting for sensors");
  const [sensors, setSensors] = useState<Record<string, SensorState>>({});
  const [rumbleSamples, setRumbleSamples] = useState<Sample[]>([]);
  const [totalSamples, setTotalSamples] = useState<Sample[]>([]);

  useEffect(() => {
    fetch("/api/demo/state")
      .then((response) => response.json())
      .then((body) => body.demo && setDemo(body.demo))
      .catch(() => undefined);

    const events = new EventSource("/events");
    events.addEventListener("open", () => setConnected(true));
    events.addEventListener("error", () => setConnected(false));
    events.addEventListener("message", (message) => {
      const event = JSON.parse(message.data) as EventPayload;
      if (event.type === "demo") {
        setDemo(event);
        setRumbleSamples((samples) => pushSample(samples, Number(event.duty || 0)));
      }
      if (event.type === "sensorStatus") {
        if (event.state === "connected") setSensorStatus(`${event.name || event.port} connected`);
        else if (event.state === "serial-ready") setSensorStatus("Serial sensors ready");
        else if (event.state === "ble-ready") setSensorStatus("BLE scan ready");
        else if (event.state === "error") setSensorStatus(event.message || "Sensor error");
        else setSensorStatus(event.state.replaceAll("_", " "));
      }
      if (event.type === "classification") {
        setClassification(event);
        setTotalSamples((samples) => pushSample(samples, event.totalShake));
      }
      if (event.type === "sensor" && (event.packet === "accel" || event.packet === "wide61")) {
        setSensors((current) => {
          const id = event.sensorId;
          const previous = current[id];
          const value = Number(event.accelMag || 0);
          return {
            ...current,
            [id]: {
              id,
              name: event.sensorName || id,
              label: floorLabels[id] || event.sensorName || id,
              connected: true,
              value,
              tempC: event.tempC,
              packet: event.packet,
              lastSeen: Date.now(),
              samples: pushSample(previous?.samples || [], value, event.ts),
            },
          };
        });
      }
    });
    return () => events.close();
  }, []);

  useEffect(() => {
    const interval = window.setInterval(() => {
      setSensors((current) => {
        const next = { ...current };
        for (const [id, sensor] of Object.entries(next)) {
          next[id] = { ...sensor, connected: Date.now() - sensor.lastSeen < 2500 };
        }
        return next;
      });
    }, 1000);
    return () => window.clearInterval(interval);
  }, []);

  const orderedSensors = useMemo(() => {
    const ordered = floorOrder.map((id) => sensors[id] || {
      id,
      name: id,
      label: floorLabels[id],
      connected: false,
      value: 0,
      packet: "",
      samples: [],
      lastSeen: 0,
    });
    const dynamicSensors = Object.values(sensors)
      .filter((sensor) => !floorOrder.includes(sensor.id))
      .sort((a, b) => a.id.localeCompare(b.id))
      .map((sensor, index) => ({
        ...sensor,
        label: sensor.label === sensor.name ? `BLE Floor ${index + 1}` : sensor.label,
      }));
    return dynamicSensors.length > 0 ? dynamicSensors.slice(0, 3) : ordered;
  }, [sensors]);

  async function startDemo() {
    const response = await fetch("/api/demo/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ duration: 20, delayMin: 5, delayMax: 20, minDuty: 0.05, maxDuty: 1, cycle: 0.2, step: 0.25 }),
    });
    const body = await response.json();
    if (!body.ok) setSensorStatus(body.error || "Start failed");
  }

  async function stopDemo() {
    await fetch("/api/demo/stop", { method: "POST" });
  }

  const activeSensors = orderedSensors.filter((sensor) => sensor.connected).length;
  const totalShake = classification?.totalShake || orderedSensors.reduce((sum, sensor) => sum + sensor.value, 0);
  const busy = demo.state === "armed" || demo.state === "waiting_random_delay" || demo.state === "running";

  return (
    <main className="shell">
      <section className="topbar">
        <div>
          <p className="eyebrow">Wendy Studio</p>
          <h1>Vibration Tower</h1>
        </div>
        <div className="connection" data-connected={connected}>
          <span />
          {connected ? "Live" : "Reconnecting"}
        </div>
      </section>

      <section className="toolbar">
        <div>
          <strong>{statusText(demo)}</strong>
          <span>{sensorStatus}</span>
        </div>
        <div className="actions">
          <button className="button primary" onClick={startDemo} disabled={busy} title="Start demo">
            <Play size={18} />
            Start
          </button>
          <button className="button" onClick={stopDemo} disabled={!busy} title="Stop demo">
            <CircleStop size={18} />
            Stop
          </button>
        </div>
      </section>

      <section className="metrics">
        <article>
          <Waves size={18} />
          <span>Classification</span>
          <strong>{classification?.label || "No motion"}</strong>
        </article>
        <article>
          <Activity size={18} />
          <span>Total shake</span>
          <strong>{totalShake.toFixed(3)}</strong>
        </article>
        <article>
          <RadioTower size={18} />
          <span>Sensors</span>
          <strong>{activeSensors}/3</strong>
        </article>
        <article>
          <AlertTriangle size={18} />
          <span>Motor duty</span>
          <strong>{Math.round((demo.duty || 0) * 100)}%</strong>
        </article>
      </section>

      <section className="panel">
        <header>
          <h2>Demo Timeline</h2>
          <span>{demo.dryRun ? "Dry run" : "Hardware"}</span>
        </header>
        <Chart samples={rumbleSamples} color="#2563eb" max={1} label="Waiting for demo" />
      </section>

      <section className="panel">
        <header>
          <h2>Total Building Response</h2>
          <span>{classification ? `${Math.round(classification.confidence * 100)}% confidence` : "No classifier signal"}</span>
        </header>
        <Chart samples={totalSamples} color="#0f766e" label="Waiting for sensor data" />
      </section>

      <section className="sensorGrid">
        {orderedSensors.map((sensor, index) => (
          <article className="sensorCard" key={sensor.id}>
            <header>
              <div>
                <h3>{sensor.label}</h3>
                <span>{sensor.id}</span>
              </div>
              <strong data-online={sensor.connected}>{sensor.connected ? "Connected" : "Offline"}</strong>
            </header>
            <Chart samples={sensor.samples} color={["#08916f", "#b45309", "#7c3aed"][index]} label="Waiting for data" />
            <footer>
              <span>{sensor.value.toFixed(3)}</span>
              <span>{sensor.tempC == null ? "--" : `${sensor.tempC.toFixed(1)}C`}</span>
            </footer>
          </article>
        ))}
      </section>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
