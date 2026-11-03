import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type MeetingSummary } from "../lib/api";
import { clock, when } from "../lib/format";
import { Empty, Page, ProgressLine, StatusPill } from "../components/ui";

const LIVE_STATUSES = new Set(["joining", "recording", "processing"]);

export default function Meetings() {
  const [meetings, setMeetings] = useState<MeetingSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [showJoin, setShowJoin] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [showUpload, setShowUpload] = useState(false);

  const load = async () => {
    try {
      setMeetings(await api.meetings());
      setError("");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  // Poll only while something is mid-flight, so an idle tab is silent.
  useEffect(() => {
    if (!meetings.some((m) => LIVE_STATUSES.has(m.status))) return;
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, [meetings]);

  return (
    <Page
      title="Meetings"
      subtitle="Send the notetaker into a call, or import a transcript you already have."
      actions={
        <div className="flex gap-2">
          <button className="btn-ghost" onClick={() => setShowImport((v) => !v)}>
            Import transcript
          </button>
          <button className="btn-ghost" onClick={() => setShowUpload((v) => !v)}>
            Upload recording
          </button>
          <button className="btn-primary" onClick={() => setShowJoin((v) => !v)}>
            Send bot to a meeting
          </button>
        </div>
      }
    >
      {showJoin && <JoinForm onDone={load} />}
      {showImport && <ImportForm onDone={load} />}
      {showUpload && <UploadForm onDone={load} />}

      {error && (
        <div className="mb-4 rounded-lg border border-red-900/60 bg-red-950/40 px-4 py-3 text-sm text-red-300">
          {error}
        </div>
      )}

      {loading ? (
        <p className="text-sm text-slate-500">Loading…</p>
      ) : meetings.length === 0 ? (
        <Empty
          title="No meetings yet"
          hint="Paste a Google Meet, Zoom or Teams link above to send the notetaker in."
        />
      ) : (
        <div className="space-y-3">
          {meetings.map((meeting) => (
            <Link
              key={meeting.id}
              to={`/meetings/${meeting.id}`}
              className="card block transition hover:border-ink-600"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2.5">
                    <h3 className="truncate font-medium text-white">{meeting.title}</h3>
                    <StatusPill status={meeting.status} />
                  </div>
                  <p className="mt-1.5 line-clamp-2 max-w-2xl text-sm text-slate-400">
                    {meeting.summary || meeting.error || "Waiting for the intelligence pass…"}
                  </p>
                  <ProgressLine progress={meeting.progress} />
                </div>
                <div className="flex shrink-0 items-center gap-5 text-sm">
                  {meeting.engagement_score != null && (
                    <Metric label="Engagement" value={`${meeting.engagement_score}`} />
                  )}
                  {meeting.meeting_score != null && (
                    <Metric label="Meeting score" value={`${meeting.meeting_score}`} />
                  )}
                  <Metric
                    label="Actions"
                    value={`${meeting.pushed_count}/${meeting.action_item_count}`}
                  />
                </div>
              </div>
              <div className="mt-3 flex flex-wrap gap-4 border-t border-ink-800 pt-3 text-xs text-slate-500">
                <span>{when(meeting.started_at ?? meeting.created_at)}</span>
                <span>{clock(meeting.duration_seconds)}</span>
                <span>{meeting.platform.replace("_", " ")}</span>
                {meeting.speaker_count != null && <span>{meeting.speaker_count} speakers</span>}
              </div>
            </Link>
          ))}
        </div>
      )}
    </Page>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="text-right">
      <div className="text-[11px] uppercase tracking-wide text-slate-600">{label}</div>
      <div className="font-semibold text-slate-200">{value}</div>
    </div>
  );
}

function JoinForm({ onDone }: { onDone: () => void }) {
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      await api.joinMeeting(url, title || undefined, name || undefined);
      setUrl("");
      setTitle("");
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card mb-5">
      <div className="grid gap-4 sm:grid-cols-3">
        <div className="sm:col-span-3">
          <label className="label">Meeting link</label>
          <input
            className="input"
            placeholder="https://meet.google.com/abc-defg-hij"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
          />
        </div>
        <div>
          <label className="label">Title (optional)</label>
          <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} />
        </div>
        <div>
          <label className="label">Bot display name</label>
          <input
            className="input"
            placeholder="Reed AI Notetaker"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <div className="flex items-end">
          <button className="btn-primary w-full" disabled={!url || busy} onClick={submit}>
            {busy ? "Joining…" : "Join now"}
          </button>
        </div>
      </div>
      {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
      <p className="mt-3 text-xs text-slate-600">
        The bot joins under its display name and may need a host to admit it. Make sure everyone in
        the call knows it is recording — consent rules vary by jurisdiction.
      </p>
    </div>
  );
}

function UploadForm({ onDone }: { onDone: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");

  const submit = async () => {
    if (!file) return;
    setBusy(true);
    setError("");
    setDone("");
    try {
      const result = await api.uploadRecording(file, title || file.name.replace(/\.[^.]+$/, ""));
      setDone(`Uploaded ${(result.bytes / 1024 / 1024).toFixed(1)} MB — transcribing now.`);
      setFile(null);
      setTitle("");
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card mb-5">
      <div className="grid gap-4 sm:grid-cols-3">
        <div>
          <label className="label">Recording (video or audio)</label>
          <input
            type="file"
            accept=".mp4,.mkv,.mov,.avi,.webm,.m4v,.mp3,.wav,.m4a,.aac,.ogg,.opus,.flac"
            className="block w-full text-sm text-slate-400 file:mr-3 file:rounded-lg file:border-0 file:bg-ink-700 file:px-3 file:py-2 file:text-sm file:text-slate-200"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </div>
        <div>
          <label className="label">Title (optional)</label>
          <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} />
        </div>
        <div className="flex items-end">
          <button className="btn-primary w-full" disabled={!file || busy} onClick={submit}>
            {busy ? "Uploading…" : "Upload & analyse"}
          </button>
        </div>
      </div>
      {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
      {done && <p className="mt-3 text-sm text-emerald-400">{done}</p>}
      <p className="mt-3 text-xs text-slate-600">
        The audio track is extracted from any common video or audio format (up to 2 GB), then
        transcribed and analysed exactly like a live meeting. Processing takes a few minutes for
        long recordings — the meeting card updates when it finishes.
      </p>
    </div>
  );
}

function ImportForm({ onDone }: { onDone: () => void }) {
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      await api.importTranscript(title || "Imported meeting", text);
      setText("");
      onDone();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card mb-5">
      <label className="label">Title</label>
      <input className="input mb-4" value={title} onChange={(e) => setTitle(e.target.value)} />
      <label className="label">Transcript</label>
      <textarea
        className="input h-40 font-mono text-xs"
        placeholder={"[00:12] Priya: Let's start with the migration status.\n[00:20] Sam: Backend is done."}
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
      <div className="mt-3 flex items-center gap-3">
        <button className="btn-primary" disabled={!text.trim() || busy} onClick={submit}>
          {busy ? "Processing…" : "Summarize"}
        </button>
        <span className="text-xs text-slate-600">
          Accepts `[MM:SS] Speaker: text`, `Speaker: text`, or plain prose.
        </span>
      </div>
      {error && <p className="mt-3 text-sm text-red-400">{error}</p>}
    </div>
  );
}
