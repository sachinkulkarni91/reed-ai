import type { ReactNode } from "react";
import type { MeetingStatus, SpeakerStats } from "../lib/api";
import { clock, speakerColor } from "../lib/format";

const STATUS_STYLES: Record<MeetingStatus, string> = {
  scheduled: "bg-slate-500/15 text-slate-300",
  joining: "bg-amber-500/15 text-amber-300",
  recording: "bg-rose-500/15 text-rose-300",
  processing: "bg-reed-500/15 text-reed-400",
  completed: "bg-emerald-500/15 text-emerald-300",
  failed: "bg-red-500/15 text-red-300",
};

export function StatusPill({ status }: { status: MeetingStatus }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[11px] font-medium ${STATUS_STYLES[status]}`}
    >
      {status === "recording" && (
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
      )}
      {status}
    </span>
  );
}

export function Page({
  title,
  subtitle,
  actions,
  children,
}: {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="mx-auto max-w-6xl px-6 py-8">
      <header className="mb-7 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-white">{title}</h1>
          {subtitle && <p className="mt-1 text-sm text-slate-500">{subtitle}</p>}
        </div>
        {actions}
      </header>
      {children}
    </div>
  );
}

export function Stat({
  label,
  value,
  hint,
}: {
  label: string;
  value: ReactNode;
  hint?: string;
}) {
  return (
    <div className="card">
      <div className="text-[11px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-white">{value}</div>
      {hint && <div className="mt-1 text-xs text-slate-500">{hint}</div>}
    </div>
  );
}

export function ScoreRing({ score, label }: { score: number; label: string }) {
  const radius = 34;
  const circumference = 2 * Math.PI * radius;
  const clamped = Math.max(0, Math.min(100, score));
  const color = clamped >= 70 ? "#34d399" : clamped >= 45 ? "#fbbf24" : "#fb7185";
  return (
    <div className="flex items-center gap-3">
      <svg width="84" height="84" viewBox="0 0 84 84" role="img" aria-label={`${label}: ${clamped}`}>
        <circle cx="42" cy="42" r={radius} fill="none" stroke="#222735" strokeWidth="8" />
        <circle
          cx="42"
          cy="42"
          r={radius}
          fill="none"
          stroke={color}
          strokeWidth="8"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={circumference * (1 - clamped / 100)}
          transform="rotate(-90 42 42)"
        />
        <text x="42" y="47" textAnchor="middle" className="fill-white text-lg font-semibold">
          {clamped}
        </text>
      </svg>
      <div className="text-sm text-slate-400">{label}</div>
    </div>
  );
}

export function TalkTimeBar({ speakers }: { speakers: SpeakerStats[] }) {
  return (
    <div className="space-y-3">
      <div className="flex h-3 w-full overflow-hidden rounded-full bg-ink-800">
        {speakers.map((s) => (
          <div
            key={s.speaker}
            style={{ width: `${s.talk_share * 100}%`, background: speakerColor(s.speaker) }}
            title={`${s.speaker} · ${(s.talk_share * 100).toFixed(0)}%`}
          />
        ))}
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        {speakers.map((s) => (
          <div key={s.speaker} className="flex items-center justify-between text-sm">
            <span className="flex items-center gap-2 text-slate-300">
              <span
                className="h-2.5 w-2.5 rounded-full"
                style={{ background: speakerColor(s.speaker) }}
              />
              {s.speaker}
            </span>
            <span className="text-slate-500">
              {(s.talk_share * 100).toFixed(0)}% · {clock(s.talk_time_s)} · {s.words_per_minute} wpm
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function SentimentChart({
  points,
}: {
  points: { timestamp_s: number; sentiment: number; label: string }[];
}) {
  if (points.length < 2) {
    return <p className="text-sm text-slate-500">Not enough signal for a sentiment curve.</p>;
  }
  const width = 640;
  const height = 120;
  const maxT = Math.max(...points.map((p) => p.timestamp_s)) || 1;
  const toXY = (p: { timestamp_s: number; sentiment: number }) => [
    (p.timestamp_s / maxT) * width,
    height / 2 - (Math.max(-1, Math.min(1, p.sentiment)) * height) / 2.4,
  ];
  const path = points
    .map((p, i) => `${i === 0 ? "M" : "L"} ${toXY(p).map((n) => n.toFixed(1)).join(" ")}`)
    .join(" ");

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className="h-32 w-full"
      role="img"
      aria-label="Sentiment over the course of the meeting"
    >
      <line x1="0" y1={height / 2} x2={width} y2={height / 2} stroke="#222735" strokeWidth="1" />
      <path d={path} fill="none" stroke="#38bdf8" strokeWidth="2.5" strokeLinejoin="round" />
      {points.map((p) => {
        const [x, y] = toXY(p);
        return (
          <circle key={p.timestamp_s} cx={x} cy={y} r="3" fill="#38bdf8">
            <title>{`${clock(p.timestamp_s)} · ${p.label || p.sentiment.toFixed(2)}`}</title>
          </circle>
        );
      })}
    </svg>
  );
}

export function ProgressLine({
  progress,
}: {
  progress: { stage: string; current: number | null; total: number | null; detail: string } | null | undefined;
}) {
  if (!progress) return null;
  const pct =
    progress.current != null && progress.total ? Math.min(100, (progress.current / progress.total) * 100) : null;
  return (
    <div className="mt-2">
      <div className="flex items-center gap-2 text-xs text-reed-400">
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-reed-400" />
        {progress.stage}
        {progress.current != null && progress.total != null && (
          <span className="text-slate-500">
            {progress.current}/{progress.total}
          </span>
        )}
        {progress.detail && <span className="text-slate-500">{progress.detail}</span>}
      </div>
      {pct != null && (
        <div className="mt-1.5 h-1 w-full max-w-xs overflow-hidden rounded-full bg-ink-800">
          <div className="h-full rounded-full bg-reed-500 transition-all" style={{ width: `${pct}%` }} />
        </div>
      )}
    </div>
  );
}

export function Empty({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="card border-dashed py-12 text-center">
      <p className="text-slate-300">{title}</p>
      {hint && <p className="mt-1 text-sm text-slate-500">{hint}</p>}
    </div>
  );
}
