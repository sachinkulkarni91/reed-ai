export function clock(seconds: number | null | undefined): string {
  const total = Math.max(0, Math.round(seconds ?? 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`;
}

export function duration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.round(seconds / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

/** Backend timestamps are UTC but SQLite strips the zone marker; without a
 *  trailing Z, JS would misread them as local time (showed 2:17 pm for a
 *  7:47 pm IST upload). */
function parseUTC(iso: string): Date {
  return new Date(/[Zz]|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + "Z");
}

export function when(iso: string | null): string {
  if (!iso) return "—";
  const date = parseUTC(iso);
  const days = Math.floor((Date.now() - date.getTime()) / 86_400_000);
  if (days === 0) return `Today, ${date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`;
  if (days === 1) return "Yesterday";
  if (days < 7) return `${days} days ago`;
  return date.toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" });
}

/** Stable colour per speaker so the same voice keeps its hue across views. */
const SPEAKER_COLORS = [
  "#38bdf8",
  "#a78bfa",
  "#34d399",
  "#fbbf24",
  "#fb7185",
  "#22d3ee",
  "#f472b6",
  "#a3e635",
];

export function speakerColor(speaker: string): string {
  let hash = 0;
  for (let i = 0; i < speaker.length; i += 1) hash = (hash * 31 + speaker.charCodeAt(i)) >>> 0;
  return SPEAKER_COLORS[hash % SPEAKER_COLORS.length];
}

export function sentimentLabel(value: number): { text: string; color: string } {
  if (value > 0.35) return { text: "Positive", color: "#34d399" };
  if (value < -0.35) return { text: "Tense", color: "#fb7185" };
  return { text: "Neutral", color: "#94a3b8" };
}
