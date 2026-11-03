import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type ActionItem } from "../lib/api";
import { Empty, Page } from "../components/ui";
import { ActionRow } from "./MeetingDetail";

const FILTERS = ["All", "Open", "Pushed", "Failed"] as const;
type Filter = (typeof FILTERS)[number];

export default function ActionItems() {
  const [items, setItems] = useState<ActionItem[]>([]);
  const [filter, setFilter] = useState<Filter>("Open");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setItems(await api.actionItems());
      setError("");
    } catch (err) {
      setError((err as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const visible = useMemo(
    () =>
      items.filter((item) => {
        if (filter === "All") return true;
        if (filter === "Open") return !item.external_id && item.status !== "dismissed";
        if (filter === "Pushed") return Boolean(item.external_id);
        return item.status === "failed";
      }),
    [items, filter]
  );

  /** Push everything outstanding, one meeting at a time. */
  const pushAll = async () => {
    setBusy(true);
    setMessage("");
    setError("");
    const meetingIds = [...new Set(items.filter((i) => !i.external_id).map((i) => i.meeting_id))];
    let pushed = 0;
    const failures: string[] = [];
    for (const meetingId of meetingIds) {
      try {
        const response = await api.push(meetingId);
        pushed += response.pushed;
      } catch (err) {
        failures.push(`Meeting ${meetingId}: ${(err as Error).message}`);
      }
    }
    setMessage(`Pushed ${pushed} item${pushed === 1 ? "" : "s"} to Trello.`);
    if (failures.length) setError(failures.join(" · "));
    setBusy(false);
    load();
  };

  const outstanding = items.filter((i) => !i.external_id && i.status !== "dismissed").length;

  return (
    <Page
      title="Action items"
      subtitle="Every commitment Reed AI heard, across all meetings."
      actions={
        <button className="btn-primary" disabled={busy || outstanding === 0} onClick={pushAll}>
          {busy ? "Pushing…" : `Push all ${outstanding} to Trello`}
        </button>
      }
    >
      <div className="mb-5 flex flex-wrap items-center gap-2">
        {FILTERS.map((name) => (
          <button
            key={name}
            onClick={() => setFilter(name)}
            className={`rounded-lg px-3 py-1.5 text-sm transition ${
              filter === name
                ? "bg-ink-800 font-medium text-white"
                : "text-slate-500 hover:text-slate-300"
            }`}
          >
            {name}
          </button>
        ))}
        {message && <span className="ml-2 text-sm text-emerald-400">{message}</span>}
        {error && <span className="ml-2 text-sm text-red-400">{error}</span>}
      </div>

      {visible.length === 0 ? (
        <Empty title="Nothing here" hint="Action items appear once a meeting has been analysed." />
      ) : (
        <div className="space-y-3">
          {visible.map((item) => (
            <ActionRow key={item.id} item={item} onChange={load} showMeeting />
          ))}
        </div>
      )}
    </Page>
  );
}
