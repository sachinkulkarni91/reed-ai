export type MeetingStatus =
  | "scheduled"
  | "joining"
  | "recording"
  | "processing"
  | "completed"
  | "failed";

export interface Progress {
  stage: string;
  current: number | null;
  total: number | null;
  detail: string;
}

export interface MeetingSummary {
  id: number;
  title: string;
  status: MeetingStatus;
  platform: string;
  created_at: string;
  started_at: string | null;
  duration_seconds: number;
  error: string | null;
  summary: string;
  meeting_score: number | null;
  engagement_score: number | null;
  speaker_count: number | null;
  action_item_count: number;
  pushed_count: number;
  progress?: Progress | null;
}

export interface Segment {
  id: number;
  speaker: string;
  text: string;
  start_s: number;
  end_s: number;
  is_final: boolean;
}

export interface ActionItem {
  id: number;
  meeting_id: number;
  meeting_title?: string;
  title: string;
  description: string;
  owner: string | null;
  owner_email: string | null;
  due_date: string | null;
  priority: "low" | "medium" | "high";
  quote: string;
  timestamp_s: number;
  confidence: number;
  status: "open" | "pushed" | "failed" | "dismissed";
  external_id: string | null;
  external_url: string | null;
  push_error: string | null;
}

export interface SpeakerStats {
  speaker: string;
  talk_time_s: number;
  talk_share: number;
  words: number;
  words_per_minute: number;
  questions_asked: number;
  filler_words: number;
  longest_monologue_s: number;
  turns: number;
  sentiment: number;
  engagement: number;
  notes: string;
}

export interface Analytics {
  duration_s: number;
  total_words: number;
  silence_share: number;
  speaker_count: number;
  speakers: SpeakerStats[];
  speaker_switches: number;
  balance: number;
  interactivity: number;
  questions_total: number;
  words_per_minute: number;
  monologue_share: number;
  engagement_score: number;
  meeting_score: number;
  score_rationale: string;
  overall_sentiment: number;
  sentiment_timeline: { timestamp_s: number; sentiment: number; label: string }[];
}

export interface Intelligence {
  headline: string;
  summary: string;
  detailed_notes: string;
  recap_email: string;
  key_points: string[];
  decisions: { decision: string; context: string; timestamp_s: number }[];
  key_questions: { question: string; asked_by: string | null; answered: boolean; answer: string }[];
  risks: { risk: string; severity: string }[];
  topics: { name: string; weight: number; mentions: number }[];
  chapters: { title: string; start_s: number; end_s: number; summary: string }[];
  highlights: { quote: string; speaker: string; timestamp_s: number; why: string }[];
  analytics: Analytics;
  model_used: string;
}

export interface VoiceProfile {
  id: number;
  name: string;
  samples: number;
  updated_at: string;
}

export interface MeetingDetail {
  meeting: MeetingSummary & { meeting_url: string | null; language: string };
  segments: Segment[];
  intelligence: Intelligence | null;
  action_items: ActionItem[];
  is_live: boolean;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail ?? detail;
    } catch {
      /* keep the status text */
    }
    throw new Error(detail);
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

export const api = {
  meetings: () => request<MeetingSummary[]>("/api/meetings"),
  meeting: (id: number) => request<MeetingDetail>(`/api/meetings/${id}`),
  deleteMeeting: (id: number) => request<void>(`/api/meetings/${id}`, { method: "DELETE" }),
  clearAllMeetings: () =>
    request<{ deleted: number; files_removed: number; skipped_live: number[] }>("/api/meetings", {
      method: "DELETE",
    }),
  renameSpeaker: (id: number, old: string, newName: string) =>
    request<{ renamed_segments: number; speaker: string }>(`/api/meetings/${id}/rename-speaker`, {
      method: "POST",
      body: JSON.stringify({ old, new: newName }),
    }),
  reprocess: (id: number, retranscribe = false) =>
    request(`/api/meetings/${id}/reprocess${retranscribe ? "?retranscribe=true" : ""}`, {
      method: "POST",
    }),
  uploadRecording: async (file: File, title: string) => {
    const form = new FormData();
    form.append("file", file);
    form.append("title", title);
    const response = await fetch("/api/meetings/upload", { method: "POST", body: form });
    if (!response.ok) {
      let detail = response.statusText;
      try {
        detail = (await response.json()).detail ?? detail;
      } catch {
        /* keep status text */
      }
      throw new Error(detail);
    }
    return (await response.json()) as { id: number; bytes: number; status: string };
  },
  importTranscript: (title: string, transcript: string) =>
    request<{ id: number; segments: number }>("/api/meetings/import", {
      method: "POST",
      body: JSON.stringify({ title, transcript }),
    }),

  joinMeeting: (meeting_url: string, title?: string, display_name?: string) =>
    request<{ meeting_id: number; platform: string }>("/api/bot/join", {
      method: "POST",
      body: JSON.stringify({ meeting_url, title, display_name }),
    }),
  leaveMeeting: (id: number) => request(`/api/bot/${id}/leave`, { method: "POST" }),
  activeBots: () => request<any[]>("/api/bot/active"),

  actionItems: (meetingId?: number) =>
    request<ActionItem[]>(`/api/action-items${meetingId ? `?meeting_id=${meetingId}` : ""}`),
  updateActionItem: (id: number, patch: Partial<ActionItem>) =>
    request<ActionItem>(`/api/action-items/${id}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  deleteActionItem: (id: number) =>
    request<void>(`/api/action-items/${id}`, { method: "DELETE" }),
  push: (meeting_id: number, item_ids?: number[], list_id?: string) =>
    request<{ pushed: number; total: number; results: any[] }>("/api/action-items/push", {
      method: "POST",
      body: JSON.stringify({ meeting_id, item_ids, list_id }),
    }),

  integrations: () => request<any>("/api/integrations/status"),
  speakers: () => request<VoiceProfile[]>("/api/integrations/speakers"),
  deleteSpeaker: (id: number) =>
    request<void>(`/api/integrations/speakers/${id}`, { method: "DELETE" }),
  trelloBoards: () => request<any[]>("/api/integrations/trello/boards"),
  trelloLists: (boardId: string) =>
    request<any[]>(`/api/integrations/trello/boards/${boardId}/lists`),
  trelloTest: () => request<any>("/api/integrations/trello/test", { method: "POST" }),

  chatHistory: (meetingId: number) => request<any[]>(`/api/chat/${meetingId}/history`),
};

/** Streams a plain-text answer, calling `onChunk` as tokens arrive. */
export async function streamAnswer(
  path: string,
  question: string,
  onChunk: (text: string) => void
): Promise<void> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  if (!response.ok || !response.body) {
    throw new Error((await response.text()) || response.statusText);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    onChunk(decoder.decode(value, { stream: true }));
  }
}
