import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { Activity, AlertTriangle, Brain, CircleStop, Play, RadioTower } from "lucide-react";
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
  transport?: "serial" | "ble" | string;
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

type EventPayload = DemoState | SensorEvent | SensorStatusEvent;

type Sample = { t: number; value: number };
type MlPrediction = {
  ready: boolean;
  prediction?: string;
  displayLabel?: string;
  confidence?: number;
  probabilities?: Record<string, number>;
  samples?: number;
  message?: string;
  error?: string;
};

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

type TrainingLabel = "no_motion" | "ambient_motion" | "slap" | "earthquake";
type TrainingCounts = Record<TrainingLabel, number>;
type TrainingReadiness = {
  ready: boolean;
  connectedCount: number;
  expectedCount: number;
  missing: string[];
  staleSeconds: number;
};
type SessionRecorderStatus = {
  active: boolean;
  message?: string;
  sessionId?: string;
  startedAt?: number;
  duration?: number;
  sensorEvents?: number;
  predictionEvents?: number;
  output?: string;
  error?: string;
};

const floorLabels: Record<string, string> = {
  "/dev/ttyUSB0": "Top Floor",
  "/dev/ttyUSB1": "Middle Floor",
  "/dev/ttyUSB2": "Bottom Floor",
  "D0:99:8C:48:4D:38": "Top Floor",
  "D1:6E:A1:15:03:57": "Middle Floor",
  "FA:91:56:1E:26:15": "Bottom Floor",
};

const floorOrder = ["/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyUSB2"];
const bleFloorOrder = ["D0:99:8C:48:4D:38", "D1:6E:A1:15:03:57", "FA:91:56:1E:26:15"];

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
  const [sensorStatus, setSensorStatus] = useState("Waiting for sensors");
  const [sensors, setSensors] = useState<Record<string, SensorState>>({});
  const [rumbleSamples, setRumbleSamples] = useState<Sample[]>([]);
  const [totalSamples, setTotalSamples] = useState<Sample[]>([]);
  const [trainingStatus, setTrainingStatus] = useState("Local recorder idle");
  const [trainingCounts, setTrainingCounts] = useState<TrainingCounts>({ no_motion: 0, ambient_motion: 0, slap: 0, earthquake: 0 });
  const [trainingReadiness, setTrainingReadiness] = useState<TrainingReadiness | null>(null);
  const [mlPrediction, setMlPrediction] = useState<MlPrediction | null>(null);
  const [sessionRecorder, setSessionRecorder] = useState<SessionRecorderStatus>({ active: false, message: "ready" });
  const [sessionStatus, setSessionStatus] = useState("Session recorder idle");

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
      if (event.type === "sensor" && (event.packet === "accel" || event.packet === "wide61")) {
        setSensors((current) => {
          const id = event.sensorId;
          const previous = current[id];
          const value = Number(event.accelMag || 0);
          const next = {
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
          const total = Object.values(next).reduce((sum, sensor) => sum + sensor.value, 0);
          setTotalSamples((samples) => pushSample(samples, total, event.ts));
          return next;
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

  useEffect(() => {
    const interval = window.setInterval(() => {
      fetch("http://127.0.0.1:8765/status")
        .then((response) => response.json())
        .then((body) => {
          const recorder = body.recorder;
          if (body.sampleCounts) {
            setTrainingCounts({
              no_motion: Number(body.sampleCounts.no_motion || 0),
              ambient_motion: Number(body.sampleCounts.ambient_motion || 0),
              slap: Number(body.sampleCounts.slap || 0),
              earthquake: Number(body.sampleCounts.earthquake || 0),
            });
          }
          if (body.sensorReadiness) {
            setTrainingReadiness({
              ready: Boolean(body.sensorReadiness.ready),
              connectedCount: Number(body.sensorReadiness.connectedCount || 0),
              expectedCount: Number(body.sensorReadiness.expectedCount || 0),
              missing: Array.isArray(body.sensorReadiness.missing) ? body.sensorReadiness.missing.map(String) : [],
              staleSeconds: Number(body.sensorReadiness.staleSeconds || 0),
            });
          }
          if (body.sessionRecorder) {
            const session = body.sessionRecorder as SessionRecorderStatus;
            setSessionRecorder(session);
            if (session.active) {
              setSessionStatus(`Recording ${session.sensorEvents || 0} sensor rows, ${session.predictionEvents || 0} ML rows`);
            } else if (session.message === "complete") {
              setSessionStatus(`Saved ${session.sessionId}: ${session.sensorEvents || 0} sensor rows, ${session.predictionEvents || 0} ML rows`);
            } else if (session.message === "error") {
              setSessionStatus(session.error || "Session recorder error");
            }
          }
          if (!recorder) return;
          if (recorder.active) {
            const duration = Number(recorder.duration || 0);
            setTrainingStatus(`Recording ${recorder.label} ${duration.toFixed(1)}s (${recorder.events || 0} samples)`);
          } else if (recorder.message === "complete") {
            setTrainingStatus(`Saved ${recorder.label}: ${recorder.events || 0} samples`);
          } else if (recorder.message === "error") {
            setTrainingStatus(recorder.error || "Recorder error");
          }
        })
        .catch(() => undefined);
      fetch("http://127.0.0.1:8765/prediction")
        .then((response) => response.json())
        .then((body) => {
          if (body.prediction) setMlPrediction(body.prediction);
        })
        .catch(() => undefined);
    }, 2000);
    return () => window.clearInterval(interval);
  }, []);

  const orderedSensors = useMemo(() => {
    const serialSensors = floorOrder.map((id) => sensors[id] || {
      id,
      name: id,
      label: floorLabels[id],
      connected: false,
      value: 0,
      packet: "",
      samples: [],
      lastSeen: 0,
    });
    const serialActive = serialSensors.some((sensor) => sensor.connected);
    if (!serialActive) {
      const bleSensors = bleFloorOrder.map((id) => sensors[id] || {
        id,
        name: id,
        label: floorLabels[id],
        connected: false,
        value: 0,
        packet: "",
        samples: [],
        lastSeen: 0,
      });
      if (bleSensors.some((sensor) => sensor.connected)) return bleSensors;
    }
    const dynamicSensors = Object.values(sensors)
      .filter((sensor) => !floorOrder.includes(sensor.id))
      .sort((a, b) => a.id.localeCompare(b.id))
      .map((sensor, index) => ({
        ...sensor,
        label: sensor.label === sensor.name ? `BLE Floor ${index + 1}` : sensor.label,
      }));
    if (serialActive || dynamicSensors.length === 0) return serialSensors;
    return dynamicSensors.slice(0, 3);
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

  async function startTrainingRecording(label: TrainingLabel) {
    const labels: Record<TrainingLabel, string> = {
      no_motion: "No motion",
      ambient_motion: "Ambient motion",
      slap: "Slap / impact",
      earthquake: "Earthquake",
    };
    if (!trainingReadiness?.ready) {
      const connectedCount = trainingReadiness?.connectedCount ?? 0;
      const expectedCount = trainingReadiness?.expectedCount ?? 3;
      setTrainingStatus(`Training blocked: ${connectedCount}/${expectedCount} sensors streaming`);
      return;
    }
    setTrainingStatus(`Starting ${labels[label]} recording`);
    try {
      const response = await fetch("http://127.0.0.1:8765/record/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          label,
          triggerDemo: label === "earthquake",
          baseUrl: window.location.origin,
          notes: `dashboard ${labels[label]} recording`,
        }),
      });
      const body = await response.json();
      if (!body.ok) {
        setTrainingStatus(body.error || "Recorder rejected request");
        return;
      }
      const duration = Number(body.recorder?.duration || 0);
      setTrainingStatus(`Recording ${labels[label]} for ${duration.toFixed(1)}s`);
    } catch {
      setTrainingStatus("Start local recorder: python3 tools/local_training_recorder.py");
    }
  }

  async function toggleSessionRecording() {
    if (sessionRecorder.active) {
      try {
        const response = await fetch("http://127.0.0.1:8765/session/stop", { method: "POST" });
        const body = await response.json();
        if (!body.ok) {
          setSessionStatus(body.error || "Stop session failed");
          return;
        }
        if (body.sessionRecorder) {
          setSessionRecorder(body.sessionRecorder);
          setSessionStatus(
            `Saved ${body.sessionRecorder.sessionId}: ${body.sessionRecorder.sensorEvents || 0} sensor rows, ${body.sessionRecorder.predictionEvents || 0} ML rows`,
          );
        }
      } catch {
        setSessionStatus("Session recorder unavailable");
      }
      return;
    }
    if (!trainingReadiness?.ready) {
      const connectedCount = trainingReadiness?.connectedCount ?? 0;
      const expectedCount = trainingReadiness?.expectedCount ?? 3;
      setSessionStatus(`Recording blocked: ${connectedCount}/${expectedCount} sensors streaming`);
      return;
    }
    try {
      const response = await fetch("http://127.0.0.1:8765/session/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ notes: "dashboard sensor and ML session recording" }),
      });
      const body = await response.json();
      if (!body.ok) {
        setSessionStatus(body.error || "Start session failed");
        return;
      }
      if (body.sessionRecorder) {
        setSessionRecorder(body.sessionRecorder);
        setSessionStatus(`Recording ${body.sessionRecorder.sessionId}`);
      }
    } catch {
      setSessionStatus("Start local recorder: python3 tools/local_training_recorder.py");
    }
  }

  const activeSensors = orderedSensors.filter((sensor) => sensor.connected).length;
  const totalShake = orderedSensors.reduce((sum, sensor) => sum + sensor.value, 0);
  const busy = demo.state === "armed" || demo.state === "waiting_random_delay" || demo.state === "running";
  const mlLabel = mlPrediction?.ready
    ? mlPrediction.displayLabel || mlPrediction.prediction?.replaceAll("_", " ")
    : mlPrediction?.message || "Waiting";
  const mlConfidence = mlPrediction?.confidence == null ? null : Math.round(mlPrediction.confidence * 100);
  const trainingReady = trainingReadiness?.ready === true;
  const trainingReadinessText = trainingReadiness
    ? trainingReadiness.ready
      ? `Ready: ${trainingReadiness.connectedCount}/${trainingReadiness.expectedCount} sensors streaming`
      : `Blocked: ${trainingReadiness.connectedCount}/${trainingReadiness.expectedCount} sensors streaming`
    : "Checking sensors";
  const sessionActive = sessionRecorder.active === true;

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

      <section className="trainingBar">
        <div>
          <strong>Session Recording</strong>
          <span>{sessionStatus}</span>
        </div>
        <div className="actions">
          <button
            className={sessionActive ? "button" : "button primary"}
            onClick={toggleSessionRecording}
            disabled={!sessionActive && !trainingReady}
            title={sessionActive ? "Stop recording sensor and ML stream" : "Record sensor and ML stream"}
          >
            {sessionActive ? <CircleStop size={18} /> : <Play size={18} />}
            {sessionActive ? "Stop recording" : "Record session"}
          </button>
        </div>
      </section>

      <section className="trainingBar">
        <div>
          <strong>Training Samples</strong>
          <span>{trainingReadinessText} - {trainingStatus}</span>
        </div>
        <div className="actions">
          <button
            className="button trainingButton"
            onClick={() => startTrainingRecording("no_motion")}
            disabled={!trainingReady}
            title="Record no motion sample"
          >
            <span>No motion</span>
            <small>{trainingCounts.no_motion} samples</small>
          </button>
          <button
            className="button trainingButton"
            onClick={() => startTrainingRecording("ambient_motion")}
            disabled={!trainingReady}
            title="Record ambient motion sample"
          >
            <span>Ambient motion</span>
            <small>{trainingCounts.ambient_motion} samples</small>
          </button>
          <button
            className="button trainingButton"
            onClick={() => startTrainingRecording("slap")}
            disabled={!trainingReady}
            title="Record slap or impact sample"
          >
            <span>Slap / impact</span>
            <small>{trainingCounts.slap} samples</small>
          </button>
          <button
            className="button trainingButton"
            onClick={() => startTrainingRecording("earthquake")}
            disabled={!trainingReady}
            title="Record earthquake sample"
          >
            <span>Earthquake</span>
            <small>{trainingCounts.earthquake} samples</small>
          </button>
        </div>
      </section>

      <section className="metrics">
        <article>
          <Brain size={18} />
          <span>ML confidence{mlConfidence == null ? "" : ` ${mlConfidence}%`}</span>
          <strong>{mlLabel}</strong>
          <small>{mlPrediction?.samples ? `${mlPrediction.samples} samples` : mlPrediction?.error || "5s window"}</small>
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
          <span>{activeSensors}/3 sensors</span>
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
