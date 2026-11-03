import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { Page } from "../components/ui";

export default function Settings() {
  const [status, setStatus] = useState<any>(null);
  const [boards, setBoards] = useState<any[]>([]);
  const [lists, setLists] = useState<any[]>([]);
  const [board, setBoard] = useState("");
  const [test, setTest] = useState("");
  const [error, setError] = useState("");
  const [clearMsg, setClearMsg] = useState("");

  const clearAll = async () => {
    if (!confirm("Delete ALL meetings, transcripts, analyses, action items and recordings? This cannot be undone."))
      return;
    setClearMsg("");
    setError("");
    try {
      const r = await api.clearAllMeetings();
      setClearMsg(
        `Deleted ${r.deleted} meeting${r.deleted === 1 ? "" : "s"} and ${r.files_removed} recording file${r.files_removed === 1 ? "" : "s"}.` +
          (r.skipped_live.length ? ` Skipped ${r.skipped_live.length} live meeting(s) - stop them first.` : "")
      );
    } catch (err) {
      setError((err as Error).message);
    }
  };

  useEffect(() => {
    api.integrations().then(setStatus).catch((err) => setError(err.message));
  }, []);

  const loadBoards = async () => {
    setError("");
    try {
      setBoards(await api.trelloBoards());
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const chooseBoard = async (id: string) => {
    setBoard(id);
    setLists(id ? await api.trelloLists(id) : []);
  };

  const runTest = async () => {
    setTest("");
    setError("");
    try {
      const result = await api.trelloTest();
      setTest(`Connected. Board ${result.board_id}, members: ${result.members.join(", ") || "none"}`);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <Page title="Settings" subtitle="Configured through backend/.env — this screen verifies it.">
      {error && (
        <div className="mb-5 rounded-lg border border-red-900/60 bg-red-950/40 px-4 py-3 text-sm text-red-300">
          {error}
        </div>
      )}

      {status && (
        <div className="grid gap-5 lg:grid-cols-2">
          <Section
            title="Claude"
            ok={status.claude.configured}
            rows={[["Model", status.claude.model], ["Key", status.claude.configured ? "set" : "missing"]]}
            note={status.claude.note}
          />
          <Section
            title="Transcription"
            ok={status.transcription.configured}
            rows={[
              ["Provider", status.transcription.provider],
              ["Model", status.transcription.model],
              ["Diarization", status.transcription.diarization ? "on (pyannote)" : "off"],
            ]}
            note={
              !status.transcription.configured
                ? "Set DEEPGRAM_API_KEY, or switch TRANSCRIPTION_PROVIDER to whisper or mock."
                : status.transcription.note || ""
            }
          />
          <Section
            title="Meeting bot"
            ok
            rows={[
              ["Display name", status.bot.display_name],
              ["Headless", String(status.bot.headless)],
              ["Max minutes", String(status.bot.max_minutes)],
            ]}
          />
          <Section
            title="Trello"
            ok={status.trello.configured}
            rows={[
              ["List ID", status.trello.list_id || "not set"],
              ["Auto-push", String(status.trello.auto_push)],
            ]}
            note={
              status.trello.configured
                ? ""
                : "Set TRELLO_API_KEY and TRELLO_TOKEN from https://trello.com/power-ups/admin"
            }
          />
        </div>
      )}

      <div className="card mt-5">
        <h2 className="mb-3 text-sm font-semibold text-white">Find your Trello list ID</h2>
        <p className="mb-4 text-sm text-slate-400">
          Pick a board and list below, then copy the ID into <code>TRELLO_LIST_ID</code> in
          <code> backend/.env</code> and restart the API.
        </p>
        <div className="flex flex-wrap items-end gap-3">
          <button className="btn-ghost" onClick={loadBoards}>
            Load boards
          </button>
          {boards.length > 0 && (
            <select className="input max-w-xs" value={board} onChange={(e) => chooseBoard(e.target.value)}>
              <option value="">Choose a board…</option>
              {boards.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.name}
                </option>
              ))}
            </select>
          )}
          <button className="btn-ghost" onClick={runTest}>
            Test configured list
          </button>
        </div>
        {test && <p className="mt-3 text-sm text-emerald-400">{test}</p>}
        {lists.length > 0 && (
          <ul className="mt-4 space-y-1.5 text-sm">
            {lists.map((l) => (
              <li key={l.id} className="flex items-center justify-between rounded-lg bg-ink-950 px-3 py-2">
                <span className="text-slate-300">{l.name}</span>
                <code className="text-xs text-slate-500">{l.id}</code>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="card mt-5 border-red-900/50">
        <h2 className="mb-2 text-sm font-semibold text-red-300">Danger zone</h2>
        <p className="mb-4 text-sm text-slate-400">
          Permanently removes every meeting, transcript, analysis, action item, Ask-AI history and
          recording file. Live meetings are skipped. Action items already pushed to Trello stay on
          Trello.
        </p>
        <button
          className="btn border border-red-900/60 text-red-300 hover:bg-red-950/40"
          onClick={clearAll}
        >
          Clear all meetings &amp; recordings
        </button>
        {clearMsg && <p className="mt-3 text-sm text-emerald-400">{clearMsg}</p>}
      </div>
    </Page>
  );
}

function Section({
  title,
  ok,
  rows,
  note,
}: {
  title: string;
  ok: boolean;
  rows: [string, string][];
  note?: string;
}) {
  return (
    <div className="card">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-white">{title}</h2>
        <span
          className={`rounded-full px-2.5 py-0.5 text-[11px] font-medium ${
            ok ? "bg-emerald-500/15 text-emerald-300" : "bg-amber-500/15 text-amber-300"
          }`}
        >
          {ok ? "ready" : "needs setup"}
        </span>
      </div>
      <dl className="space-y-1.5 text-sm">
        {rows.map(([key, value]) => (
          <div key={key} className="flex justify-between gap-4">
            <dt className="text-slate-500">{key}</dt>
            <dd className="truncate text-slate-300">{value}</dd>
          </div>
        ))}
      </dl>
      {note && <p className="mt-3 text-xs text-amber-400/80">{note}</p>}
    </div>
  );
}
