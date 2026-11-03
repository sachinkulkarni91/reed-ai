import { useEffect, useState } from "react";
import { api, type VoiceProfile } from "../lib/api";
import { Empty, Page } from "../components/ui";
import { speakerColor, when } from "../lib/format";

export default function Speakers() {
  const [profiles, setProfiles] = useState<VoiceProfile[] | null>(null);
  const [error, setError] = useState("");

  const load = () =>
    api
      .speakers()
      .then(setProfiles)
      .catch((err) => setError((err as Error).message));

  useEffect(() => {
    load();
  }, []);

  const remove = async (p: VoiceProfile) => {
    if (
      !confirm(
        `Forget ${p.name}'s voice? They will show up as "Speaker N" in future meetings until tagged again.`
      )
    )
      return;
    try {
      await api.deleteSpeaker(p.id);
      load();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <Page
      title="Speakers"
      subtitle="Voices Reed AI recognises automatically — no screenshots or introductions needed."
    >
      {error && (
        <div className="mb-5 rounded-lg border border-red-900/60 bg-red-950/40 px-4 py-3 text-sm text-red-300">
          {error}
        </div>
      )}

      <div className="card mb-5">
        <h2 className="mb-2 text-sm font-semibold text-white">How voices get learned</h2>
        <p className="text-sm leading-relaxed text-slate-400">
          Whenever a speaker in a meeting gets a real name — from the participant list on screen, from
          the conversation itself, or from you tagging them in the transcript — Reed AI saves a compact
          voice fingerprint under that name. In every future meeting, each voice is checked against this
          registry first, so known people are named instantly. Fingerprints improve with every meeting a
          person is tagged in: 2–3 tagged meetings per person makes recognition very reliable.
        </p>
        <p className="mt-2 text-sm text-slate-400">
          To add someone: open any meeting they spoke in, go to the <b>Transcript</b> tab and click
          their speaker label (or use the <b>Tag speakers</b> row) to give them their real name.
        </p>
      </div>

      {profiles === null ? (
        <p className="text-sm text-slate-500">Loading…</p>
      ) : profiles.length === 0 ? (
        <Empty
          title="No voices enrolled yet"
          hint="Tag speakers in any meeting's transcript and they will appear here."
        />
      ) : (
        <div className="card overflow-x-auto">
          <table className="w-full min-w-[520px] text-sm">
            <thead className="text-left text-[11px] uppercase tracking-wide text-slate-500">
              <tr>
                <th className="pb-2">Name</th>
                <th className="pb-2">Training strength</th>
                <th className="pb-2">Last updated</th>
                <th className="pb-2"></th>
              </tr>
            </thead>
            <tbody className="text-slate-300">
              {profiles.map((p) => (
                <tr key={p.id} className="border-t border-ink-800">
                  <td className="py-2.5">
                    <span className="flex items-center gap-2">
                      <span
                        className="grid h-7 w-7 shrink-0 place-items-center rounded-full text-xs font-semibold text-white"
                        style={{ backgroundColor: speakerColor(p.name) }}
                      >
                        {p.name
                          .split(/\s+/)
                          .slice(0, 2)
                          .map((w) => w[0]?.toUpperCase())
                          .join("")}
                      </span>
                      {p.name}
                    </span>
                  </td>
                  <td>
                    <span className="flex items-center gap-2">
                      <span className="flex gap-0.5">
                        {[1, 2, 3, 4, 5].map((i) => (
                          <span
                            key={i}
                            className={`h-1.5 w-4 rounded-full ${
                              p.samples >= i ? "bg-emerald-500/80" : "bg-ink-800"
                            }`}
                          />
                        ))}
                      </span>
                      <span className="text-xs text-slate-500">
                        {p.samples} meeting{p.samples === 1 ? "" : "s"}
                      </span>
                    </span>
                  </td>
                  <td className="text-slate-400">{when(p.updated_at)}</td>
                  <td className="text-right">
                    <button
                      className="text-xs text-red-400/80 hover:text-red-300 hover:underline"
                      onClick={() => remove(p)}
                    >
                      Forget voice
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Page>
  );
}
