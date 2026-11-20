"use client";

import { useEffect, useRef, useState } from "react";

import { useAuth } from "@/components/AuthProvider";
import { RequireAuth } from "@/components/RequireAuth";
import { API_BASE, readError } from "@/lib/api";
import { makeRoster, TelemetrySimulator } from "@/lib/simulator";
import type { FleetDeviceRecord, SimDevice } from "@/lib/types";

const MIN_INTERVAL_SEC = 3;
const MAX_INTERVAL_SEC = 60;
const MAX_LOG_ENTRIES = 50;

type LogEntry = {
  id: number;
  time: string;
  ok: boolean;
  text: string;
};

async function uploadErrorLines(response: Response): Promise<string[]> {
  try {
    const body = (await response.json()) as { detail?: { errors?: { row: number | null; error: string }[] } };
    return (body.detail?.errors ?? []).slice(0, 20).map((item) =>
      item.row != null ? `Row ${item.row}: ${item.error}` : item.error
    );
  } catch {
    return [];
  }
}

function SourceWorkspace() {
  const { authorizedFetch } = useAuth();
  const [running, setRunning] = useState(false);
  const [intervalSec, setIntervalSec] = useState(5);
  const [rosterSize, setRosterSize] = useState(5);
  const [pushes, setPushes] = useState(0);
  const [deviceCount, setDeviceCount] = useState(0);
  const [log, setLog] = useState<LogEntry[]>([]);
  const [error, setError] = useState("");

  const timerRef = useRef<number | null>(null);
  const inFlightRef = useRef(false);
  const simulatorRef = useRef<TelemetrySimulator | null>(null);
  const nextLogId = useRef(1);

  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadPreview, setUploadPreview] = useState<string[]>([]);
  const [uploadBusy, setUploadBusy] = useState(false);
  const [uploadMessage, setUploadMessage] = useState("");
  const [uploadOk, setUploadOk] = useState(false);
  const [uploadErrors, setUploadErrors] = useState<string[]>([]);

  const chooseUploadFile = async (file: File | null) => {
    setUploadFile(file);
    setUploadMessage("");
    setUploadErrors([]);
    if (!file) {
      setUploadPreview([]);
      return;
    }
    const text = await file.text();
    setUploadPreview(text.split(/\r?\n/).filter((line) => line.trim() !== "").slice(0, 6));
  };

  const submitUpload = async () => {
    if (!uploadFile) {
      return;
    }
    setUploadBusy(true);
    setUploadMessage("");
    setUploadErrors([]);
    setUploadOk(false);
    try {
      const isJson = uploadFile.name.toLowerCase().endsWith(".json");
      const response = await authorizedFetch(
        `/api/v1/fleet/uploads?filename=${encodeURIComponent(uploadFile.name)}`,
        { method: "POST", headers: { "Content-Type": isJson ? "application/json" : "text/csv" }, body: uploadFile }
      );
      if (!response.ok) {
        setUploadMessage(await readError(response));
        setUploadErrors(await uploadErrorLines(response));
        return;
      }
      const body = (await response.json()) as { snapshot_count: number; device_count: number };
      setUploadOk(true);
      setUploadMessage(`Stored ${body.snapshot_count} snapshot(s) with ${body.device_count} device reading(s) in your tenant.`);
      setUploadFile(null);
      setUploadPreview([]);
    } catch (caught) {
      setUploadMessage(caught instanceof Error ? caught.message : "Upload failed");
    } finally {
      setUploadBusy(false);
    }
  };

  const appendLog = (ok: boolean, text: string) => {
    const entry: LogEntry = {
      id: nextLogId.current++,
      time: new Date().toLocaleTimeString(),
      ok,
      text
    };
    setLog((current) => [entry, ...current].slice(0, MAX_LOG_ENTRIES));
  };

  const stop = () => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
    setRunning(false);
  };

  // Stop the feed on unmount so it does not keep posting after the user leaves the page.
  useEffect(() => {
    return () => {
      if (timerRef.current !== null) {
        window.clearInterval(timerRef.current);
      }
    };
  }, []);

  const pushSnapshot = async () => {
    if (inFlightRef.current || !simulatorRef.current) {
      return;
    }
    inFlightRef.current = true;
    try {
      const snapshot = simulatorRef.current.next(new Date());
      const response = await authorizedFetch("/api/v1/fleet/snapshots", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(snapshot)
      });
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Push failed (${response.status}): ${detail.slice(0, 160)}`);
      }
      const body = (await response.json()) as { snapshot_id: string };
      setPushes((count) => count + 1);
      appendLog(true, `Pushed ${snapshot.devices.length} device readings as snapshot ${body.snapshot_id.slice(0, 8)}`);
      setError("");
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "Push failed";
      setError(message);
      appendLog(false, message);
      stop();
    } finally {
      inFlightRef.current = false;
    }
  };

  const start = async () => {
    if (running) {
      return;
    }
    setError("");
    try {
      // Use the tenant's existing devices so the feed updates real inventory. Ingest upserts per device,
      // so a fresh roster is only needed when the tenant has no devices yet.
      const response = await authorizedFetch("/api/v1/fleet/devices");
      if (!response.ok) {
        throw new Error(`Could not load devices (${response.status}). Check the API key on the co-pilot page.`);
      }
      const existing = ((await response.json()) as { devices: FleetDeviceRecord[] }).devices;
      const roster: SimDevice[] =
        existing.length > 0
          ? existing.map((device) => ({
              device_id: device.device_id,
              employee_id: device.employee_id,
              device_type: device.device_type,
              os_name: device.os_name,
              os_version: device.os_version
            }))
          : makeRoster(Date.now() >>> 0, rosterSize);

      simulatorRef.current = new TelemetrySimulator(roster, Date.now() >>> 0);
      setDeviceCount(roster.length);
      setPushes(0);
      appendLog(
        true,
        existing.length > 0
          ? `Feed started for ${roster.length} existing device(s)`
          : `No devices yet. Feed started with ${roster.length} simulated device(s)`
      );

      const interval = Math.min(MAX_INTERVAL_SEC, Math.max(MIN_INTERVAL_SEC, intervalSec)) * 1000;
      setRunning(true);
      void pushSnapshot();
      timerRef.current = window.setInterval(() => void pushSnapshot(), interval);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not start the feed");
    }
  };

  return (
    <main className="mx-auto flex w-full max-w-5xl flex-col gap-6 p-6">
      <header className="space-y-2 pt-4">
        <p className="text-sm uppercase tracking-wide text-cyan-300">Source</p>
        <h1 className="text-3xl font-bold">Stream telemetry into the fleet</h1>
        <p className="text-slate-300">
          A simulated feed posts synthetic device snapshots to the tenant&apos;s fleet at a fixed interval. Disk and RAM
          readings drift over time, so threshold and persistence checks have movement to find. Battery health holds
          steady, because a battery trend needs weeks of history. Only synthetic data is generated.
        </p>
      </header>

      <section className="space-y-4 rounded-xl border border-slate-800 bg-panel p-5">
        <div>
          <h2 className="text-lg font-semibold text-cyan-300">Upload your data</h2>
          <p className="text-sm text-slate-400">
            JSON uses the fleet snapshot format. CSV has one device per row, grouped by observed_at. The whole file is
            checked first: if any row is wrong, nothing is stored.
          </p>
        </div>
        <input
          type="file"
          accept=".json,.csv,application/json,text/csv"
          onChange={(event) => void chooseUploadFile(event.target.files?.[0] ?? null)}
          className="block w-full text-sm text-slate-300 file:mr-4 file:rounded-md file:border-0 file:bg-slate-800 file:px-3 file:py-2 file:text-slate-100"
        />
        {uploadPreview.length > 0 && (
          <pre className="overflow-x-auto rounded-md bg-panelSoft p-3 text-xs text-slate-300">
            {uploadPreview.join("\n")}
          </pre>
        )}
        <div className="flex items-center gap-3">
          <button
            onClick={submitUpload}
            disabled={!uploadFile || uploadBusy}
            className="rounded-md bg-emerald-500 px-4 py-2 font-semibold text-slate-900 hover:bg-emerald-400 disabled:opacity-50"
          >
            {uploadBusy ? "Uploading…" : "Upload to my tenant"}
          </button>
          {uploadFile && <span className="text-xs text-slate-400">{uploadFile.name}</span>}
        </div>
        {uploadMessage && (
          <p className={`rounded-md p-3 text-sm ${uploadOk ? "bg-emerald-900/60 text-emerald-100" : "bg-rose-900/60 text-rose-100"}`}>
            {uploadMessage}
          </p>
        )}
        {uploadErrors.length > 0 && (
          <ul className="list-disc space-y-1 pl-6 text-sm text-rose-200">
            {uploadErrors.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        )}
      </section>

      <section className="grid gap-4 rounded-xl border border-slate-800 bg-panel p-5 md:grid-cols-3">
        <label className="text-sm">
          Interval (seconds)
          <input
            type="number"
            min={MIN_INTERVAL_SEC}
            max={MAX_INTERVAL_SEC}
            value={intervalSec}
            disabled={running}
            onChange={(event) => setIntervalSec(Number(event.target.value))}
            className="mt-1 w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-2 disabled:opacity-50"
          />
          <span className="mt-1 block text-xs text-slate-400">
            Minimum {MIN_INTERVAL_SEC}s, so the audit log does not grow too fast.
          </span>
        </label>
        <label className="text-sm">
          Simulated devices (only if the tenant has none)
          <input
            type="number"
            min={1}
            max={20}
            value={rosterSize}
            disabled={running}
            onChange={(event) => setRosterSize(Math.min(20, Math.max(1, Number(event.target.value))))}
            className="mt-1 w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-2 disabled:opacity-50"
          />
        </label>
        <div className="flex flex-col justify-end gap-2">
          {running ? (
            <button
              onClick={stop}
              className="rounded-md bg-rose-500 px-4 py-2 font-semibold text-white transition hover:bg-rose-400"
            >
              Stop feed
            </button>
          ) : (
            <button
              onClick={start}
              className="rounded-md bg-cyan-500 px-4 py-2 font-semibold text-slate-900 transition hover:bg-cyan-400"
            >
              Start feed
            </button>
          )}
        </div>
      </section>

      <section className="grid gap-4 md:grid-cols-3">
        <div className="rounded-xl border border-slate-800 bg-panel p-4">
          <p className="text-xs uppercase text-slate-400">Status</p>
          <p className="mt-1 text-lg font-semibold">{running ? "Streaming" : "Stopped"}</p>
        </div>
        <div className="rounded-xl border border-slate-800 bg-panel p-4">
          <p className="text-xs uppercase text-slate-400">Snapshots pushed</p>
          <p className="mt-1 text-lg font-semibold">{pushes}</p>
        </div>
        <div className="rounded-xl border border-slate-800 bg-panel p-4">
          <p className="text-xs uppercase text-slate-400">Devices in feed</p>
          <p className="mt-1 text-lg font-semibold">{deviceCount}</p>
        </div>
      </section>

      {error && <p className="rounded-md bg-rose-900/60 p-3 text-sm text-rose-100">{error}</p>}

      <section className="rounded-xl border border-slate-800 bg-panel p-4">
        <h2 className="mb-3 text-lg font-semibold text-cyan-300">Live push log</h2>
        {log.length === 0 && <p className="text-sm text-slate-400">Nothing pushed yet.</p>}
        <ul className="space-y-1 font-mono text-xs">
          {log.map((entry) => (
            <li key={entry.id} className={entry.ok ? "text-slate-200" : "text-rose-300"}>
              <span className="text-slate-500">{entry.time}</span> {entry.text}
            </li>
          ))}
        </ul>
      </section>
    </main>
  );
}

export default function SourcePage() {
  return (
    <RequireAuth>
      <SourceWorkspace />
    </RequireAuth>
  );
}
