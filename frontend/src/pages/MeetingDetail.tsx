import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import AskAI from "../components/AskAI";
import {
  Empty,
  Page,
  ProgressLine,
  ScoreRing,
  SentimentChart,
  Stat,
  StatusPill,
  TalkTimeBar,
} from "../components/ui";
import { api, type ActionItem, type MeetingDetail, type Segment } from "../lib/api";
import { clock, sentimentLabel, speakerColor, when } from "../lib/format";

const TABS = ["Overview", "Transcript", "Action items", "Analytics", "Ask AI"] as const;
type Tab = (typeof TABS)[number];

export default function MeetingDetailPage() {
  const { id } = useParams();
  const meetingId = Number(id);
  const navigate = useNavigate();
  const [data, setData] = useState<MeetingDetail | null>(null);
  const [tab, setTab] = useState<Tab>("Overview");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setData(await api.meeting(meetingId));
      setError("");
    } catch (err) {
      setError((err as Error).message);
    }
  }, [meetingId]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    const status = data?.meeting.status;
    if (status !== "joining" && status !== "processing") return;
    const timer = setInterval(load, 4000);
    return () => clearInterval(timer);
  }, [data?.meeting.status, load]);

  if (error) return <Page title="Meeting"><p className="text-red-400">{error}</p></Page>;
  if (!data) return <Page title="Meeting"><p className="text-slate-500">Loading…</p></Page>;

  const { meeting, intelligence, segments, action_items, is_live } = data;

  return (
    <Page
      title={meeting.title}
      subtitle={`${when(meeting.started_at ?? meeting.created_at)} · ${clock(
        meeting.duration_seconds
      )} · ${meeting.platform.replace("_", " ")}`}
      actions={
        <div className="flex items-center gap-2">
          <StatusPill status={meeting.status} />
          {is_live && (
            <button className="btn-ghost" onClick={() => api.leaveMeeting(meetingId).then(load)}>
              Stop recording
            </button>
          )}
          <button className="btn-ghost" onClick={() => api.reprocess(meetingId).then(load)}>
            Re-run analysis
          </button>
          <button
            className="btn-ghost"
            title="Drops the stored transcript and transcribes the kept recording again - use after enabling diarization or changing provider"
            onClick={() => {
              if (confirm("Re-transcribe from the recording? The current transcript will be replaced."))
                api.reprocess(meetingId, true).then(load);
            }}
          >
            Re-transcribe
          </button>
          <button
            className="btn-ghost text-red-400 hover:border-red-900 hover:text-red-300"
            onClick={() => {
              if (confirm("Delete this meeting, its transcript, analysis, action items and recording? This cannot be undone."))
                api.deleteMeeting(meetingId).then(() => navigate("/meetings"));
            }}
          >
            Delete
          </button>
          <Link className="btn-ghost" to="/meetings">
            Back
          </Link>
        </div>
      }
    >
      {meeting.progress && (
        <div className="-mt-3 mb-5">
          <ProgressLine progress={meeting.progress} />
        </div>
      )}

      {meeting.error && (
        <div className="mb-5 rounded-lg border border-red-900/60 bg-red-950/40 px-4 py-3 text-sm text-red-300">
          {meeting.error}
        </div>
      )}

      <div className="mb-6 flex gap-1 border-b border-ink-800">
        {TABS.map((name) => (
          <button
            key={name}
            onClick={() => setTab(name)}
            className={`-mb-px border-b-2 px-4 py-2.5 text-sm transition ${
              tab === name
                ? "border-reed-500 font-medium text-white"
                : "border-transparent text-slate-500 hover:text-slate-300"
            }`}
          >
            {name}
            {name === "Action items" && action_items.length > 0 && (
              <span className="ml-2 rounded-full bg-ink-800 px-1.5 py-0.5 text-[11px] text-slate-400">
                {action_items.length}
              </span>
            )}
          </button>
        ))}
      </div>

      {tab === "Overview" && <Overview intelligence={intelligence} />}
      {tab === "Transcript" && (
        <Transcript meetingId={meetingId} segments={segments} live={is_live} />
      )}
      {tab === "Action items" && (
        <ActionItemsTab meetingId={meetingId} items={action_items} onChange={load} />
      )}
      {tab === "Analytics" && <AnalyticsTab intelligence={intelligence} />}
      {tab === "Ask AI" && (
        <AskAI
          meetingId={meetingId}
          endpoint={`/api/chat/${meetingId}/ask`}
          placeholder="Ask about this meeting…"
        />
      )}
    </Page>
  );
}

function Overview({ intelligence }: { intelligence: MeetingDetail["intelligence"] }) {
  const [copied, setCopied] = useState(false);
  if (!intelligence) {
    return <Empty title="No summary yet" hint="The intelligence pass runs once the meeting ends." />;
  }

  return (
    <div className="grid gap-5 lg:grid-cols-3">
      <div className="space-y-5 lg:col-span-2">
        <section className="card">
          <h2 className="mb-2 text-sm font-semibold text-white">Summary</h2>
          <p className="text-sm leading-relaxed text-slate-300">{intelligence.summary}</p>
        </section>

        {intelligence.key_points.length > 0 && (
          <section className="card">
            <h2 className="mb-3 text-sm font-semibold text-white">Key points</h2>
            <ul className="space-y-2">
              {intelligence.key_points.map((point, i) => (
                <li key={i} className="flex gap-2.5 text-sm text-slate-300">
                  <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-reed-500" />
                  {point}
                </li>
              ))}
            </ul>
          </section>
        )}

        {intelligence.decisions.length > 0 && (
          <section className="card">
            <h2 className="mb-3 text-sm font-semibold text-white">Decisions</h2>
            <div className="space-y-3">
              {intelligence.decisions.map((d, i) => (
                <div key={i} className="border-l-2 border-emerald-500/60 pl-3">
                  <p className="text-sm text-slate-200">{d.decision}</p>
                  {d.context && <p className="mt-0.5 text-xs text-slate-500">{d.context}</p>}
                  <span className="text-[11px] text-slate-600">{clock(d.timestamp_s)}</span>
                </div>
              ))}
            </div>
          </section>
        )}

        {intelligence.chapters.length > 0 && (
          <section className="card">
            <h2 className="mb-3 text-sm font-semibold text-white">Chapters</h2>
            <div className="space-y-3">
              {intelligence.chapters.map((c, i) => (
                <div key={i} className="flex gap-4">
                  <span className="w-14 shrink-0 font-mono text-xs text-slate-500">
                    {clock(c.start_s)}
                  </span>
                  <div>
                    <p className="text-sm font-medium text-slate-200">{c.title}</p>
                    <p className="text-xs text-slate-500">{c.summary}</p>
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}

        {intelligence.detailed_notes && (
          <section className="card">
            <h2 className="mb-3 text-sm font-semibold text-white">Detailed notes</h2>
            <pre className="whitespace-pre-wrap font-sans text-sm leading-relaxed text-slate-300">
              {intelligence.detailed_notes}
            </pre>
          </section>
        )}
      </div>

      <div className="space-y-5">
        {intelligence.key_questions.length > 0 && (
          <section className="card">
            <h2 className="mb-3 text-sm font-semibold text-white">Key questions</h2>
            <div className="space-y-3">
              {intelligence.key_questions.map((q, i) => (
                <div key={i}>
                  <p className="text-sm text-slate-200">{q.question}</p>
                  <p className="mt-0.5 text-xs text-slate-500">
                    {q.asked_by ? `${q.asked_by} · ` : ""}
                    {q.answered ? q.answer || "Answered" : "Unanswered"}
                  </p>
                </div>
              ))}
            </div>
          </section>
        )}

        {intelligence.risks.length > 0 && (
          <section className="card">
            <h2 className="mb-3 text-sm font-semibold text-white">Risks raised</h2>
            <ul className="space-y-2 text-sm">
              {intelligence.risks.map((r, i) => (
                <li key={i} className="flex items-start gap-2">
                  <span
                    className={`mt-1 h-2 w-2 shrink-0 rounded-full ${
                      r.severity === "high"
                        ? "bg-red-500"
                        : r.severity === "low"
                          ? "bg-slate-500"
                          : "bg-amber-500"
                    }`}
                  />
                  <span className="text-slate-300">{r.risk}</span>
                </li>
              ))}
            </ul>
          </section>
        )}

        {intelligence.topics.length > 0 && (
          <section className="card">
            <h2 className="mb-3 text-sm font-semibold text-white">Topics</h2>
            <div className="flex flex-wrap gap-2">
              {intelligence.topics.map((t) => (
                <span
                  key={t.name}
                  className="rounded-full border border-ink-700 px-2.5 py-1 text-xs text-slate-300"
                  title={`${t.mentions} mentions`}
                >
                  {t.name}
                </span>
              ))}
            </div>
          </section>
        )}

        {intelligence.highlights.length > 0 && (
          <section className="card">
            <h2 className="mb-3 text-sm font-semibold text-white">Highlights</h2>
            <div className="space-y-3">
              {intelligence.highlights.map((h, i) => (
                <blockquote key={i} className="border-l-2 border-reed-600 pl-3">
                  <p className="text-sm italic text-slate-300">“{h.quote}”</p>
                  <p className="mt-1 text-[11px] text-slate-500">
                    {h.speaker} · {clock(h.timestamp_s)} — {h.why}
                  </p>
                </blockquote>
              ))}
            </div>
          </section>
        )}

        {intelligence.recap_email && (
          <section className="card">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-sm font-semibold text-white">Recap email</h2>
              <button
                className="btn-ghost text-xs"
                onClick={() => {
                  navigator.clipboard.writeText(intelligence.recap_email);
                  setCopied(true);
                  setTimeout(() => setCopied(false), 1500);
                }}
              >
                {copied ? "Copied" : "Copy"}
              </button>
            </div>
            <pre className="whitespace-pre-wrap font-sans text-xs leading-relaxed text-slate-400">
              {intelligence.recap_email}
            </pre>
          </section>
        )}
      </div>
    </div>
  );
}

function Transcript({
  meetingId,
  segments,
  live,
}: {
  meetingId: number;
  segments: Segment[];
  live: boolean;
}) {
  const [liveLines, setLiveLines] = useState<Segment[]>([]);
  const [interim, setInterim] = useState<string>("");
  const [query, setQuery] = useState("");

  useEffect(() => {
    if (!live) return;
    const source = new EventSource(`/api/meetings/${meetingId}/live`);
    source.onmessage = (event) => {
      const payload = JSON.parse(event.data);
      if (payload.type !== "segment") return;
      if (payload.is_final) {
        setInterim("");
        setLiveLines((prev) => [...prev, payload as Segment]);
      } else {
        setInterim(payload.text);
      }
    };
    source.onerror = () => source.close();
    return () => source.close();
  }, [live, meetingId]);

  const all = [...segments, ...liveLines];
  const filtered = query
    ? all.filter((s) => s.text.toLowerCase().includes(query.toLowerCase()))
    : all;

  const uniqueSpeakers = [...new Set(all.map((s) => s.speaker))];
  const tagSpeaker = (speaker: string) => {
    const name = prompt(
      `Who is "${speaker}"? Their real name is applied everywhere in this meeting and their voice is saved so Reed AI recognises them automatically in future meetings.`,
      speaker.startsWith("Speaker ") ? "" : speaker
    );
    if (name && name.trim() && name.trim() !== speaker)
      api.renameSpeaker(meetingId, speaker, name.trim()).then(() => window.location.reload());
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <input
          className="input max-w-xs"
          placeholder="Search the transcript…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <a className="btn-ghost" href={`/api/meetings/${meetingId}/transcript.txt`}>
          Export .txt
        </a>
        <a className="btn-ghost" href={`/api/meetings/${meetingId}/transcript.vtt`}>
          Export .vtt
        </a>
        {live && (
          <span className="flex items-center gap-2 text-xs text-rose-300">
            <span className="h-2 w-2 animate-pulse rounded-full bg-rose-400" /> Live
          </span>
        )}
      </div>

      {uniqueSpeakers.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[11px] uppercase tracking-wide text-slate-500">Tag speakers</span>
          {uniqueSpeakers.map((sp) => {
            const untagged = /^Speaker \d+$/.test(sp);
            return (
              <button
                key={sp}
                onClick={() => tagSpeaker(sp)}
                title={
                  untagged
                    ? "Click to identify this person — their voice will be remembered for future meetings"
                    : "Voice saved — click to correct the name"
                }
                className={`rounded-full border px-2.5 py-1 text-xs transition ${
                  untagged
                    ? "border-amber-500/40 bg-amber-500/10 text-amber-300 hover:bg-amber-500/20"
                    : "border-ink-700 bg-ink-900 text-slate-300 hover:bg-ink-800"
                }`}
                style={untagged ? undefined : { color: speakerColor(sp) }}
              >
                {untagged ? `${sp} — who is this?` : `${sp} ✓`}
              </button>
            );
          })}
        </div>
      )}

      {filtered.length === 0 && !interim ? (
        <Empty
          title={query ? "No matching lines" : live ? "Listening…" : "No transcript captured"}
          hint={
            live
              ? "Live captions appear a few seconds after someone speaks. The full transcript is written when the meeting ends."
              : undefined
          }
        />
      ) : (
        <div className="card max-h-[70vh] space-y-3 overflow-y-auto">
          {filtered.map((segment, i) => (
            <div key={segment.id ?? `live-${i}`} className="flex gap-4">
              <span className="w-12 shrink-0 pt-0.5 font-mono text-xs text-slate-600">
                {clock(segment.start_s)}
              </span>
              <div>
                <button
                  className="text-xs font-medium hover:underline"
                  style={{ color: speakerColor(segment.speaker) }}
                  title="Click to rename this speaker everywhere in the meeting"
                  onClick={() => {
                    const name = prompt(`Rename "${segment.speaker}" everywhere in this meeting to:`, segment.speaker);
                    if (name && name.trim() && name !== segment.speaker)
                      api.renameSpeaker(meetingId, segment.speaker, name.trim()).then(() => window.location.reload());
                  }}
                >
                  {segment.speaker}
                </button>
                <p className="text-sm leading-relaxed text-slate-300">{segment.text}</p>
              </div>
            </div>
          ))}
          {interim && (
            <p className="border-l-2 border-rose-500/50 pl-3 text-sm italic leading-relaxed text-slate-400">
              {interim}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function ActionItemsTab({
  meetingId,
  items,
  onChange,
}: {
  meetingId: number;
  items: ActionItem[];
  onChange: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState("");
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<number[]>([]);

  const pushable = items.filter((i) => !i.external_id && i.status !== "dismissed");

  const push = async (ids?: number[]) => {
    setBusy(true);
    setError("");
    setResult("");
    try {
      const response = await api.push(meetingId, ids);
      setResult(`Pushed ${response.pushed} of ${response.total} to Trello.`);
      setSelected([]);
      onChange();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (items.length === 0) {
    return <Empty title="No action items" hint="Nothing in this meeting read as a commitment." />;
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <button className="btn-primary" disabled={busy || pushable.length === 0} onClick={() => push()}>
          {busy ? "Pushing…" : `Push ${pushable.length} to Trello`}
        </button>
        {selected.length > 0 && (
          <button className="btn-ghost" disabled={busy} onClick={() => push(selected)}>
            Push {selected.length} selected
          </button>
        )}
        {result && <span className="text-sm text-emerald-400">{result}</span>}
        {error && <span className="text-sm text-red-400">{error}</span>}
      </div>

      <div className="space-y-3">
        {items.map((item) => (
          <ActionRow
            key={item.id}
            item={item}
            selected={selected.includes(item.id)}
            onToggle={() =>
              setSelected((prev) =>
                prev.includes(item.id) ? prev.filter((id) => id !== item.id) : [...prev, item.id]
              )
            }
            onChange={onChange}
          />
        ))}
      </div>
    </div>
  );
}

export function ActionRow({
  item,
  selected,
  onToggle,
  onChange,
  showMeeting,
}: {
  item: ActionItem;
  selected?: boolean;
  onToggle?: () => void;
  onChange: () => void;
  showMeeting?: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item);

  const save = async () => {
    await api.updateActionItem(item.id, {
      title: draft.title,
      owner: draft.owner,
      due_date: draft.due_date,
      priority: draft.priority,
    });
    setEditing(false);
    onChange();
  };

  const priorityColor =
    item.priority === "high"
      ? "text-red-400"
      : item.priority === "low"
        ? "text-slate-500"
        : "text-amber-400";

  return (
    <div className="card">
      <div className="flex items-start gap-3">
        {onToggle && !item.external_id && (
          <input
            type="checkbox"
            className="mt-1 h-4 w-4 accent-sky-500"
            checked={Boolean(selected)}
            onChange={onToggle}
          />
        )}
        <div className="min-w-0 flex-1">
          {editing ? (
            <div className="grid gap-3 sm:grid-cols-4">
              <input
                className="input sm:col-span-4"
                value={draft.title}
                onChange={(e) => setDraft({ ...draft, title: e.target.value })}
              />
              <input
                className="input"
                placeholder="Owner"
                value={draft.owner ?? ""}
                onChange={(e) => setDraft({ ...draft, owner: e.target.value })}
              />
              <input
                className="input"
                type="date"
                value={draft.due_date ?? ""}
                onChange={(e) => setDraft({ ...draft, due_date: e.target.value })}
              />
              <select
                className="input"
                value={draft.priority}
                onChange={(e) =>
                  setDraft({ ...draft, priority: e.target.value as ActionItem["priority"] })
                }
              >
                <option value="low">low</option>
                <option value="medium">medium</option>
                <option value="high">high</option>
              </select>
              <div className="flex gap-2">
                <button className="btn-primary" onClick={save}>
                  Save
                </button>
                <button className="btn-ghost" onClick={() => setEditing(false)}>
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <>
              <p className="font-medium text-slate-100">{item.title}</p>
              {item.description && (
                <p className="mt-1 text-sm text-slate-400">{item.description}</p>
              )}
              {item.quote && (
                <p className="mt-2 border-l-2 border-ink-700 pl-3 text-xs italic text-slate-500">
                  “{item.quote}”
                </p>
              )}
              <div className="mt-2.5 flex flex-wrap items-center gap-3 text-xs text-slate-500">
                <span className={priorityColor}>{item.priority}</span>
                <span>{item.owner ?? "Unassigned"}</span>
                {item.due_date && <span>due {item.due_date}</span>}
                <span>{clock(item.timestamp_s)}</span>
                <span>{Math.round(item.confidence * 100)}% confidence</span>
                {showMeeting && item.meeting_title && (
                  <Link className="hover:text-slate-300" to={`/meetings/${item.meeting_id}`}>
                    {item.meeting_title}
                  </Link>
                )}
              </div>
            </>
          )}
        </div>

        <div className="flex shrink-0 flex-col items-end gap-2">
          {item.external_url ? (
            <a
              className="text-xs text-emerald-400 hover:underline"
              href={item.external_url}
              target="_blank"
              rel="noreferrer"
            >
              On Trello ↗
            </a>
          ) : (
            <span className="text-[11px] uppercase tracking-wide text-slate-600">{item.status}</span>
          )}
          {!editing && (
            <button className="text-xs text-slate-500 hover:text-slate-300" onClick={() => setEditing(true)}>
              Edit
            </button>
          )}
          <button
            className="text-xs text-slate-600 hover:text-red-400"
            onClick={() => api.deleteActionItem(item.id).then(onChange)}
          >
            Delete
          </button>
        </div>
      </div>
      {item.push_error && <p className="mt-2 text-xs text-red-400">{item.push_error}</p>}
    </div>
  );
}

function AnalyticsTab({ intelligence }: { intelligence: MeetingDetail["intelligence"] }) {
  if (!intelligence?.analytics) {
    return <Empty title="No analytics yet" />;
  }
  const a = intelligence.analytics;
  const mood = sentimentLabel(a.overall_sentiment ?? 0);

  return (
    <div className="space-y-5">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Stat label="Duration" value={clock(a.duration_s)} hint={`${a.total_words} words`} />
        <Stat
          label="Talk balance"
          value={`${Math.round((a.balance ?? 0) * 100)}%`}
          hint={`${a.speaker_count} speakers, ${a.speaker_switches} switches`}
        />
        <Stat
          label="Pace"
          value={`${a.words_per_minute} wpm`}
          hint={`${Math.round((a.silence_share ?? 0) * 100)}% silence`}
        />
        <Stat
          label="Sentiment"
          value={<span style={{ color: mood.color }}>{mood.text}</span>}
          hint={`${a.questions_total} questions asked`}
        />
      </div>

      <div className="grid gap-5 lg:grid-cols-3">
        <div className="card flex items-center justify-around lg:col-span-1">
          <ScoreRing score={a.engagement_score ?? 0} label="Engagement" />
          <ScoreRing score={a.meeting_score ?? 0} label="Effectiveness" />
        </div>
        <div className="card lg:col-span-2">
          <h2 className="mb-3 text-sm font-semibold text-white">Talk time</h2>
          <TalkTimeBar speakers={a.speakers ?? []} />
        </div>
      </div>

      {a.score_rationale && (
        <div className="card">
          <h2 className="mb-2 text-sm font-semibold text-white">Why this score</h2>
          <p className="text-sm text-slate-400">{a.score_rationale}</p>
        </div>
      )}

      <div className="card">
        <h2 className="mb-3 text-sm font-semibold text-white">Sentiment over time</h2>
        <SentimentChart points={a.sentiment_timeline ?? []} />
      </div>

      <div className="card overflow-x-auto">
        <h2 className="mb-3 text-sm font-semibold text-white">Per-speaker breakdown</h2>
        <table className="w-full min-w-[640px] text-sm">
          <thead className="text-left text-[11px] uppercase tracking-wide text-slate-500">
            <tr>
              <th className="pb-2">Speaker</th>
              <th className="pb-2">Share</th>
              <th className="pb-2">WPM</th>
              <th className="pb-2">Questions</th>
              <th className="pb-2">Longest turn</th>
              <th className="pb-2">Fillers</th>
              <th className="pb-2">Engagement</th>
            </tr>
          </thead>
          <tbody className="text-slate-300">
            {(a.speakers ?? []).map((s) => (
              <tr key={s.speaker} className="border-t border-ink-800">
                <td className="py-2" style={{ color: speakerColor(s.speaker) }}>
                  {s.speaker}
                </td>
                <td>{Math.round(s.talk_share * 100)}%</td>
                <td>{s.words_per_minute}</td>
                <td>{s.questions_asked}</td>
                <td>{clock(s.longest_monologue_s)}</td>
                <td>{s.filler_words}</td>
                <td>{Math.round(s.engagement * 100)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
