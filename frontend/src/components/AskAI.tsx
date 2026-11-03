import { useEffect, useRef, useState } from "react";
import { api, streamAnswer } from "../lib/api";

interface Turn {
  role: "user" | "assistant";
  content: string;
}

const SUGGESTIONS = [
  "What did I commit to?",
  "What was decided, and what was left open?",
  "Summarise the objections raised",
  "What should I follow up on first?",
];

export default function AskAI({
  meetingId,
  endpoint,
  placeholder,
  suggestions = SUGGESTIONS,
}: {
  meetingId?: number;
  endpoint: string;
  placeholder: string;
  suggestions?: string[];
}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (meetingId === undefined) return;
    api
      .chatHistory(meetingId)
      .then((rows) => setTurns(rows.map((r) => ({ role: r.role, content: r.content }))))
      .catch(() => undefined);
  }, [meetingId]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns]);

  const ask = async (text: string) => {
    if (!text.trim() || busy) return;
    setBusy(true);
    setError("");
    setQuestion("");
    setTurns((prev) => [...prev, { role: "user", content: text }, { role: "assistant", content: "" }]);
    try {
      await streamAnswer(endpoint, text, (chunk) => {
        setTurns((prev) => {
          const next = [...prev];
          next[next.length - 1] = {
            role: "assistant",
            content: next[next.length - 1].content + chunk,
          };
          return next;
        });
      });
    } catch (err) {
      setError((err as Error).message);
      setTurns((prev) => prev.slice(0, -1));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card flex h-[560px] flex-col">
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto pr-1">
        {turns.length === 0 && (
          <div className="pt-6 text-center">
            <p className="text-sm text-slate-500">Ask anything about this content.</p>
            <div className="mt-4 flex flex-wrap justify-center gap-2">
              {suggestions.map((s) => (
                <button key={s} className="btn-ghost text-xs" onClick={() => ask(s)}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}
        {turns.map((turn, i) => (
          <div
            key={i}
            className={turn.role === "user" ? "flex justify-end" : "flex justify-start"}
          >
            <div
              className={`max-w-[85%] whitespace-pre-wrap rounded-xl px-4 py-2.5 text-sm leading-relaxed ${
                turn.role === "user"
                  ? "bg-reed-600 text-white"
                  : "border border-ink-700 bg-ink-950 text-slate-200"
              }`}
            >
              {turn.content || <span className="text-slate-500">Thinking…</span>}
            </div>
          </div>
        ))}
        <div ref={bottom} />
      </div>

      {error && <p className="mt-3 text-sm text-red-400">{error}</p>}

      <form
        className="mt-4 flex gap-2 border-t border-ink-800 pt-4"
        onSubmit={(e) => {
          e.preventDefault();
          ask(question);
        }}
      >
        <input
          className="input"
          placeholder={placeholder}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
        />
        <button className="btn-primary" disabled={busy || !question.trim()}>
          {busy ? "…" : "Ask"}
        </button>
      </form>
    </div>
  );
}
