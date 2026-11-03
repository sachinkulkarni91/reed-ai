"""Name speakers by reading the meeting video itself.

Meeting recordings (Teams, Meet, Zoom) render the active speaker's tile with a
name label. We already know *when* each diarized speaker talks, so:

  1. pick frames from the middle of each unnamed speaker's longest turns,
  2. ask Claude vision whose name label is on the actively-speaking tile,
  3. majority-vote the sightings per diarized speaker into a name.

Runs only for speakers the conversational evidence didn't already name, and
only when the meeting has a video file (bot meetings capture audio only).
A wrong name in minutes is worse than a label, so a speaker keeps 'Speaker N'
unless the frames agree.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

from ..config import settings
from .pipeline import SegmentView, fmt_ts

log = logging.getLogger(__name__)

MAX_FRAMES = 12
FRAMES_PER_SPEAKER = 3
MIN_TURN_S = 3.0
FRAME_WIDTH = 1024


class FrameSighting(BaseModel):
    frame_index: int = Field(description="0-based index of the frame as given.")
    name: Optional[str] = Field(
        default=None,
        description="Name label on the tile of the person actively speaking / "
        "highlighted / shown largest. null if no name is legible or no one is "
        "clearly the active speaker.",
    )


class FrameSightings(BaseModel):
    frames: list[FrameSighting] = Field(default_factory=list)


def _pick_sample_times(views: list[SegmentView], unnamed: list[str]) -> list[tuple[float, str]]:
    """(timestamp, diarized-speaker) pairs at the middle of long turns."""
    samples: list[tuple[float, str]] = []
    per_speaker = max(1, min(FRAMES_PER_SPEAKER, MAX_FRAMES // max(1, len(unnamed))))
    for speaker in unnamed:
        turns = sorted(
            (v for v in views if v.speaker == speaker and v.end_s - v.start_s >= MIN_TURN_S),
            key=lambda v: v.start_s - v.end_s,  # longest first
        )
        for view in turns[:per_speaker]:
            samples.append(((view.start_s + view.end_s) / 2, speaker))
    return samples[:MAX_FRAMES]


def _extract_frames(video_path: str, times: list[float]) -> list[bytes | None]:
    """JPEG bytes for each requested timestamp (None where extraction fails)."""
    import av
    from PIL import Image

    frames: list[bytes | None] = []
    with av.open(video_path) as container:
        stream = container.streams.video[0]
        for t in times:
            try:
                container.seek(int(t * av.time_base), backward=True)
                image = None
                for frame in container.decode(stream):
                    if frame.time is not None and frame.time >= t - 0.5:
                        image = frame.to_image()
                        break
                if image is None:
                    frames.append(None)
                    continue
                if image.width > FRAME_WIDTH:
                    image = image.resize(
                        (FRAME_WIDTH, int(image.height * FRAME_WIDTH / image.width))
                    )
                buf = io.BytesIO()
                image.convert("RGB").save(buf, format="JPEG", quality=80)
                frames.append(buf.getvalue())
            except Exception as exc:
                log.debug("frame extraction failed at %.1fs: %s", t, exc)
                frames.append(None)
    return frames


def has_video_stream(path) -> bool:
    """Bot recordings are .webm audio-only; extension checks lie. Ask the container."""
    try:
        import av

        with av.open(str(path)) as container:
            return len(container.streams.video) > 0
    except Exception:
        return False


_STATUS_TOKENS = ("muted", "speaking", "camera", "mic", "pinned", "spotlight",
                  "hand raised", "organizer", "organiser", "presenter", "attendee", "guest")


def _clean_name(raw: str | None) -> str | None:
    """Strip UI status words while preserving 'Last, First' tile labels.

    'Kapil, muted, is speaking'  -> 'Kapil'
    'Tarnaich, Mehak'            -> 'Tarnaich, Mehak'  (flipped later by
    normalize_screen_name - a naive comma-split here cost us first names.)
    """
    if not raw:
        return None
    parts = [p.strip() for p in str(raw).split(",")]
    kept = [
        p for p in parts
        if p and not any(tok in p.lower() for tok in _STATUS_TOKENS)
    ]
    if not kept:
        return None
    name = ", ".join(kept[:2]) if len(kept) >= 2 else kept[0]
    if len(name) >= 60:
        return None
    if settings.bot_display_name.lower() in name.lower():
        return None  # never name a speaker after the bot itself
    return name


def normalize_screen_name(name: str) -> str:
    """Meeting tiles often label people 'Last, First' - flip to 'First Last'."""
    parts = [p.strip() for p in name.split(",")]
    if len(parts) == 2 and all(parts):
        return f"{parts[1]} {parts[0]}"
    return name


def merge_names(conv_map: dict[str, str], screen_map: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Combine conversation-inferred and screen-read names.

    Screen labels win: the conversation layer HEARS names (phonetic - 'Mehek'
    for a tile that reads 'Mehak'), while tiles carry the authoritative
    spelling. When both exist and clearly refer to the same person, the
    conversational spelling becomes an alias so prose already written with it
    gets corrected too. Returns (label->name, misspelling->correct-name).
    """
    import difflib

    screen = {k: normalize_screen_name(v) for k, v in screen_map.items()}
    final: dict[str, str] = {}
    alias: dict[str, str] = {}
    for label in set(conv_map) | set(screen):
        c, s = conv_map.get(label), screen.get(label)
        if s and c:
            final[label] = s
            similar = any(
                difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() >= 0.6
                for a in c.split()
                for b in s.split()
            )
            if similar and c != s:
                alias[c] = s
        elif s:
            final[label] = s
        elif c:
            final[label] = c
    return _dedupe(final), alias


def _dedupe(mapping: dict[str, str]) -> dict[str, str]:
    """Two diarized speakers must never map to one name - drop collisions."""
    seen: set[str] = set()
    out: dict[str, str] = {}
    for speaker, name in mapping.items():
        if name not in seen:
            seen.add(name)
            out[speaker] = name
    return out


def vote_dom_samples(
    samples: list[dict], views: list[SegmentView], unnamed: list[str]
) -> dict[str, str]:
    """Map diarized speakers to the live bot's DOM active-speaker samples.

    Free and exact: while in the call, the bot recorded (t, name) whenever the
    meeting page marked someone as speaking. A sample landing inside a diarized
    speaker's turn is a vote for that name; a clear majority (>=2 and >50%)
    names the speaker.
    """
    votes: dict[str, dict[str, int]] = {}
    for sample in samples:
        t = sample.get("t")
        name = _clean_name(sample.get("name"))
        if t is None or not name:
            continue
        for view in views:
            if view.speaker in unnamed and view.start_s <= float(t) <= view.end_s:
                votes.setdefault(view.speaker, {})
                votes[view.speaker][name] = votes[view.speaker].get(name, 0) + 1
                break
    mapping: dict[str, str] = {}
    for speaker, counts in votes.items():
        best, count = max(counts.items(), key=lambda kv: kv[1])
        if len(counts) == 1:
            # Unanimous - even one sample is the page's own text, not a guess.
            mapping[speaker] = best
        elif count >= 2 and count > sum(counts.values()) / 2:
            mapping[speaker] = best
    mapping = _dedupe(mapping)
    if mapping:
        log.info("DOM samples named speakers: %s", mapping)
    return mapping


async def infer_names_from_snapshots(
    frames_dir, views: list[SegmentView], unnamed: list[str]
) -> dict[str, str]:
    """Vision naming from the bot's periodic in-meeting screenshots (t{sec}.jpg)."""
    from pathlib import Path as _Path

    shots: list[tuple[float, _Path]] = []
    for f in _Path(frames_dir).glob("t*.jpg"):
        try:
            shots.append((float(f.stem[1:]), f))
        except ValueError:
            continue
    if not shots:
        return {}
    shots.sort()

    pairs: list[tuple[float, bytes, str]] = []
    per_speaker: dict[str, int] = {}
    for speaker in unnamed:
        for view in sorted(
            (v for v in views if v.speaker == speaker), key=lambda v: v.start_s - v.end_s
        ):
            for t, f in shots:
                if view.start_s <= t <= view.end_s and per_speaker.get(speaker, 0) < FRAMES_PER_SPEAKER:
                    pairs.append((t, f.read_bytes(), speaker))
                    per_speaker[speaker] = per_speaker.get(speaker, 0) + 1
            if per_speaker.get(speaker, 0) >= FRAMES_PER_SPEAKER:
                break
    return await _identify_pairs(pairs[:MAX_FRAMES])


async def infer_names_from_video(
    video_path: str, views: list[SegmentView], unnamed: list[str]
) -> dict[str, str]:
    """Returns {diarized label -> name} for speakers the video can identify."""
    samples = _pick_sample_times(views, unnamed)
    if not samples:
        return {}
    frames = await asyncio.to_thread(_extract_frames, video_path, [t for t, _ in samples])
    pairs = [
        (t, jpeg, speaker)
        for (t, speaker), jpeg in zip(samples, frames)
        if jpeg is not None
    ]
    return await _identify_pairs(pairs)


async def _identify_pairs(pairs: list[tuple[float, bytes, str]]) -> dict[str, str]:
    """Shared core: frames -> Claude vision -> per-speaker majority vote."""
    content: list[dict] = []
    kept: list[tuple[int, str]] = []  # (content frame index, diarized speaker)
    for t, jpeg, speaker in pairs:
        if jpeg is None:
            continue
        index = len(kept)
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.standard_b64encode(jpeg).decode(),
                },
            }
        )
        content.append({"type": "text", "text": f"Frame {index} (at {fmt_ts(t)})"})
        kept.append((index, speaker))
    if not kept:
        return {}

    content.append(
        {
            "type": "text",
            "text": (
                "These are frames from a recorded video meeting. In each frame, "
                "identify the participant who is actively speaking - the tile that "
                "is highlighted, outlined, or shown largest - and read the name "
                "label on that tile. Return one entry per frame. Use null when no "
                "name is legible or no one is clearly the active speaker. Report "
                "names exactly as written on screen."
            ),
        }
    )

    client = (
        anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        if settings.anthropic_api_key
        else anthropic.AsyncAnthropic()
    )
    response = await client.messages.parse(
        model=settings.reed_model,
        max_tokens=2000,
        messages=[{"role": "user", "content": content}],
        output_format=FrameSightings,
    )
    sightings = {f.frame_index: _clean_name(f.name) for f in response.parsed_output.frames}

    # Majority vote per diarized speaker; ties or single dissent -> stay unnamed.
    votes: dict[str, dict[str, int]] = {}
    for index, speaker in kept:
        name = sightings.get(index)
        if name:
            votes.setdefault(speaker, {}).setdefault(name, 0)
            votes[speaker][name] += 1

    mapping: dict[str, str] = {}
    for speaker, counts in votes.items():
        best, best_count = max(counts.items(), key=lambda kv: kv[1])
        total = sum(counts.values())
        if best_count >= max(2, (total // 2) + 1) or (total == 1 and best_count == 1):
            mapping[speaker] = best
    mapping = _dedupe(mapping)
    if mapping:
        log.info("vision named speakers: %s", mapping)
    return mapping
