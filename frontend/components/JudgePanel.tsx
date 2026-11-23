"use client";

import { FormEvent, useEffect, useState } from "react";

import { useAuth } from "@/components/AuthProvider";
import { readError } from "@/lib/api";

type KeyStatus = { configured: boolean; last4?: string; expires_at?: string };
type JudgeResult = { scores: Record<string, number>; judge_model: string; note: string };

type Props = {
  question: string;
  canJudge: boolean;
};

const TTL_OPTIONS = [7, 30, 90];

export function JudgePanel({ question, canJudge }: Props) {
  const { authorizedFetch, session } = useAuth();
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<KeyStatus>({ configured: false });
  const [keyInput, setKeyInput] = useState("");
  const [ttlDays, setTtlDays] = useState(30);
  const [useSaved, setUseSaved] = useState(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [result, setResult] = useState<JudgeResult | null>(null);
  const isUser = session !== null && session.email !== "";

  const loadStatus = async () => {
    const response = await authorizedFetch("/api/v1/judge-key");
    if (response.ok) {
      setStatus((await response.json()) as KeyStatus);
    }
  };

  useEffect(() => {
    if (open && isUser) {
      void loadStatus();
    }
    // Reload when the panel opens or the signed-in account changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, isUser]);

  const saveKey = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    setMessage("");
    const response = await authorizedFetch("/api/v1/judge-key", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ openai_api_key: keyInput.trim(), ttl_days: ttlDays }),
    });
    setKeyInput("");
    if (!response.ok) {
      setError(await readError(response));
    } else {
      setStatus((await response.json()) as KeyStatus);
      setMessage(`Key saved. It expires in ${ttlDays} days unless you delete it sooner.`);
    }
    setBusy(false);
  };

  const deleteKey = async () => {
    setBusy(true);
    setError("");
    await authorizedFetch("/api/v1/judge-key", { method: "DELETE" });
    setStatus({ configured: false });
    setMessage("Saved key deleted.");
    setBusy(false);
  };

  const runJudge = async () => {
    setBusy(true);
    setError("");
    setResult(null);
    const body: Record<string, string> = { question };
    if (!useSaved || !status.configured) {
      body.openai_api_key = keyInput.trim();
    }
    const response = await authorizedFetch("/api/v1/evals/grounding-judge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    setKeyInput("");
    if (!response.ok) {
      setError(await readError(response));
    } else {
      setResult((await response.json()) as JudgeResult);
    }
    setBusy(false);
  };

  return (
    <section className="rounded-xl border border-slate-800 bg-panel p-4 text-sm">
      <button onClick={() => setOpen(!open)} className="font-semibold text-cyan-300">
        {open ? "Hide" : "Show"} grounding judge (optional, uses your OpenAI key)
      </button>
      {open && (
        <div className="mt-4 space-y-4">
          <p className="text-slate-400">
            Scores how well the answer is supported by its citations, using RAGAS and OpenAI. It is off by default.
            The question, the answer, and the cited evidence text are sent to OpenAI. Raw telemetry rows are not.
          </p>
          {!isUser && <p className="text-amber-300">Sign in with a user account to save a key. You can still enter one for a single run.</p>}

          <div className="rounded-md bg-panelSoft p-3">
            <p>
              Saved key:{" "}
              {status.configured
                ? `ending ${status.last4}, expires ${new Date(status.expires_at ?? "").toLocaleDateString()}`
                : "none"}
            </p>
            {status.configured && (
              <button onClick={deleteKey} disabled={busy} className="mt-2 rounded-md bg-rose-600 px-3 py-1 text-white disabled:opacity-50">
                Delete saved key
              </button>
            )}
          </div>

          {isUser && (
            <form onSubmit={saveKey} className="flex flex-col gap-2 md:flex-row md:items-end">
              <label className="flex-1">
                OpenAI API key
                <input
                  type="password"
                  autoComplete="off"
                  value={keyInput}
                  onChange={(event) => setKeyInput(event.target.value)}
                  className="mt-1 w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-2"
                />
              </label>
              <label>
                Keep for
                <select
                  value={ttlDays}
                  onChange={(event) => setTtlDays(Number(event.target.value))}
                  className="mt-1 rounded-md border border-slate-700 bg-slate-900 px-3 py-2"
                >
                  {TTL_OPTIONS.map((days) => (
                    <option key={days} value={days}>
                      {days} days
                    </option>
                  ))}
                </select>
              </label>
              <button type="submit" disabled={busy || !keyInput.trim()} className="rounded-md bg-emerald-500 px-4 py-2 font-semibold text-slate-900 disabled:opacity-50">
                Save key
              </button>
            </form>
          )}

          <div className="space-y-2 border-t border-slate-800 pt-4">
            {status.configured && (
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={useSaved} onChange={(event) => setUseSaved(event.target.checked)} />
                Use the saved key for this run
              </label>
            )}
            {(!status.configured || !useSaved) && (
              <label className="block">
                One-time key for this run only (not saved)
                <input
                  type="password"
                  autoComplete="off"
                  value={keyInput}
                  onChange={(event) => setKeyInput(event.target.value)}
                  className="mt-1 w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-2"
                />
              </label>
            )}
            <button
              onClick={runJudge}
              disabled={busy || !canJudge || (!status.configured && !keyInput.trim())}
              className="rounded-md bg-cyan-500 px-4 py-2 font-semibold text-slate-900 disabled:opacity-50"
            >
              {busy ? "Running…" : "Run grounding judge"}
            </button>
            {!canJudge && <p className="text-xs text-slate-400">Ask a question that gets a cited answer first.</p>}
          </div>

          {message && <p className="rounded-md bg-emerald-900/60 p-2 text-emerald-100">{message}</p>}
          {error && <p className="rounded-md bg-rose-900/60 p-2 text-rose-100">{error}</p>}
          {result && (
            <div className="rounded-md bg-panelSoft p-3">
              <p className="font-semibold">Scores ({result.judge_model})</p>
              <ul className="mt-1">
                {Object.entries(result.scores).map(([name, value]) => (
                  <li key={name}>
                    {name}: {(value * 100).toFixed(0)}%
                  </li>
                ))}
              </ul>
              <p className="mt-2 text-xs text-slate-400">{result.note}</p>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
