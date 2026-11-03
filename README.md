# Reed AI

A meeting intelligence platform: a bot joins your Google Meet / Zoom / Teams call,
records and transcribes it with speaker diarization, and turns it into a summary,
chapters, decisions, analytics and action items — then pushes those action items
to Trello as cards.

```
 Meeting URL
     │
     ▼
 Playwright bot  ──joins the call, captures tab audio──►  /ws/ingest/{id}
                                                              │
                                     faster-whisper + pyannote (local, free)
                                       (or Deepgram streaming for live captions)
                                                              │
                                                   TranscriptSegment rows
                                                              │
                                              Claude Opus 5 (one structured pass)
                                                              │
        ┌────────────────┬────────────────┬──────────────────┴──────────┐
     summary          chapters         analytics                   action items
     decisions        highlights       talk time / engagement            │
     questions        recap email      sentiment curve            Trello cards
```

## What it does

**Capture**
- Bot joins Google Meet, Zoom (web client) and Microsoft Teams as a named guest
- Live tab-audio capture, streamed to the backend ~4× per second
- Free local transcription (faster-whisper) with pyannote speaker diarization — the default
- Optional Deepgram streaming mode for live captions in the UI while the call runs
- Upload a recording (MP4/MKV/MOV/WebM video or MP3/WAV/M4A audio, up to 2 GB) -
  the audio track is extracted and analysed exactly like a live meeting
- Import an existing transcript instead of recording one
- Export as `.txt` or WebVTT

**Understand** (one Claude Opus 5 structured-output pass per meeting)
- Headline, summary, detailed notes, ready-to-send recap email
- Key points, decisions, key questions (with whether they were answered), risks
- Topics, chapters with real timestamps, quotable highlights
- Sentiment timeline and per-speaker sentiment
- Effectiveness score with a written rationale

**Measure** (computed in Python, not asked of the model)
- Talk time and talk share per speaker, words per minute, filler-word counts
- Longest monologue, turn counts, speaker switches, silence share
- Talk balance, interactivity, and a 0–100 engagement score

**Act**
- Action items with owner, due date, priority, source quote, timestamp, confidence
- Editable before they leave the building
- Push to Trello: one card per item, with due date, assignee matched against board
  members, a priority label, the source quote as a comment, and a link back to the
  meeting. Idempotent — re-pushing never duplicates a card
- Optional auto-push the moment analysis finishes (`TRELLO_AUTO_PUSH=true`)

**Ask**
- Ask-AI copilot per meeting, streamed, with the transcript prompt-cached so
  follow-up questions are cheap
- Search across every analysed meeting

## Setup

### Backend

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate      # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium               # only needed for the live bot
cp .env.example .env                                # then fill in your keys
uvicorn app.main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. The Vite dev server proxies `/api` and `/ws` to port 8000.

### Keys

| Variable | Needed for | Where to get it |
|---|---|---|
| `ANTHROPIC_API_KEY` | all summarization and Ask-AI | console.anthropic.com |
| `HF_TOKEN` | speaker diarization (free) | huggingface.co → Settings → Access Tokens, then accept the terms at hf.co/pyannote/speaker-diarization-3.1 |
| `TRELLO_API_KEY`, `TRELLO_TOKEN` | pushing action items | https://trello.com/power-ups/admin |
| `TRELLO_LIST_ID` | which list cards land on | Settings → *Find your Trello list ID* |
| `DEEPGRAM_API_KEY` | only if you switch to live captions | console.deepgram.com |

Nothing is hardcoded: **Settings** shows exactly what is configured and what is missing.

## Transcription modes

**`whisper` (default, free).** Audio is recorded during the meeting and
transcribed locally with faster-whisper once the bot leaves — no transcription
API, no per-minute cost. With `HF_TOKEN` set, pyannote assigns speaker labels
("who spoke when"); without it every line is "Speaker 0" and talk-time analytics
are not meaningful. First run downloads the models (~1 GB for diarization).
Tradeoff: no live captions — the transcript appears a minute or two after the
meeting ends.

**`sarvam`.** For Hinglish / code-mixed Indian meetings. Live captions stay on
local Whisper; the final transcript comes from Sarvam Saaras in `codemix` mode
(built for Indic-English mixing, which Whisper garbles), chunked at silence
boundaries and re-assembled with word timestamps. Speaker labels still come
from local pyannote (Sarvam's REST endpoint does not diarize). Falls back to
local Whisper automatically if the API is unreachable. Needs `SARVAM_API_KEY`
from dashboard.sarvam.ai.

**`deepgram`.** Streaming transcription with diarization (~$0.26/hr): live
captions appear in the UI while the call is running.

**`mock`.** Replays a scripted two-speaker conversation on a realistic cadence,
so the whole pipeline can be exercised end to end with no keys at all.

## API

| Method | Path | |
|---|---|---|
| `POST` | `/api/bot/join` | send the bot to a meeting URL |
| `POST` | `/api/bot/{id}/leave` | pull it out |
| `GET` | `/api/bot/active` | live sessions and bytes received |
| `GET` | `/api/meetings` | list with scores and action-item counts |
| `GET` | `/api/meetings/{id}` | meeting, transcript, intelligence, action items |
| `POST` | `/api/meetings/upload` | analyse an uploaded video/audio recording |
| `POST` | `/api/meetings/import` | analyse a transcript you already have |
| `POST` | `/api/meetings/{id}/reprocess` | re-run the intelligence pass |
| `GET` | `/api/meetings/{id}/live` | SSE stream of live captions |
| `GET` | `/api/meetings/{id}/transcript.{txt,vtt}` | export |
| `GET/PATCH/DELETE` | `/api/action-items[/{id}]` | review and edit |
| `POST` | `/api/action-items/push` | push to Trello |
| `POST` | `/api/chat/{id}/ask` | streamed Q&A about one meeting |
| `POST` | `/api/chat/search` | streamed Q&A across all meetings |
| `GET` | `/api/integrations/status` | what is configured |
| `WS` | `/ws/ingest/{id}` | audio ingest (used by the bot) |

## Things worth knowing

**Recording consent.** The bot joins under a visible display name and does not
hide that it is there, but it does not ask for consent on your behalf. Recording
rules differ by jurisdiction and by platform terms of service — announce it.

**Platform fragility.** Meeting UIs change without notice, so the join flows in
`backend/app/bot/playwright_bot.py` are selector-based and will need occasional
maintenance. Run with `BOT_HEADLESS=false` to watch a join and see where it
sticks. Google Meet's "Ask to join" means a host must admit the bot; the bot
waits two minutes, then gives up and marks the meeting failed with the reason.
Every failed join saves a screenshot and the page's visible text to
`backend/data/media/meeting-{id}-join_failed.{png,txt}` so a selector fix
doesn't require guessing.

**Teams is the least reliable of the three.** Google Meet and Zoom's web
clients tolerate headless automation better than Microsoft's does. Two distinct
Teams failure modes have shown up in testing:
- The web client can fail before any join UI even renders — its MSAL
  silent-auth iframe (`login.microsoftonline.com`, `prompt=none`) aborts inside
  a headless browser and the app hangs on its splash screen indefinitely,
  independent of any selector. If a join hangs with an empty diagnostics `.txt`
  file (nothing ever rendered), that's this. Try `BOT_HEADLESS=false` to
  confirm, but expect this one to need more than a selector fix.
- Even after joining successfully, Teams' Content-Security-Policy is a strict
  `connect-src` allow-list of Microsoft's own domains — this is why the ingest
  connection is opened by the Python bot process itself (see "How audio
  actually gets out" below) rather than by a script injected into the page.
Meet and Zoom are the platforms this approach is built for; treat Teams
support as best-effort.

**How audio actually gets out of the meeting tab.** The injected script
(`backend/app/bot/capture.js`) captures tab audio via `getDisplayMedia` and
buffers WebM/Opus chunks in memory as base64 - it does **not** open the ingest
WebSocket itself. A page-initiated connection is subject to whatever
Content-Security-Policy the meeting site sets, and Teams' `connect-src` doesn't
allow it (confirmed via its actual CSP header - the connection gets blocked
instantly, before any network round-trip). Instead, the Python bot process
opens the ingest WebSocket and pulls buffered chunks out of the page via
Playwright's `page.evaluate()` (DevTools protocol, not a page-initiated network
request, so no CSP applies) once a second. This is what makes capture CSP-proof
on every platform, not just a Teams-specific patch.

**Cost.** With the default whisper provider, transcription is free; one meeting
costs one Opus 5 extraction call. The transcript is sent as a cached prefix, so
each follow-up copilot question re-reads it at cache rates rather than full
price.

**Layout**

```
backend/app/
  bot/          Playwright join flows, tab-audio capture, live session registry
  services/     transcription providers, Claude layer, analytics, Trello, pipeline
  api/          REST routes + the audio ingest websocket
  models.py     Meeting, TranscriptSegment, MeetingIntelligence, ActionItem, ChatMessage
frontend/src/
  pages/        Meetings, MeetingDetail, ActionItems, Ask, Settings
  components/   shared UI, Ask-AI panel
```
