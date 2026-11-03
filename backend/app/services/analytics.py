"""Conversation analytics computed from the diarized transcript.

Deterministic numbers (talk time, pace, monologues, interactivity) are computed
here rather than asked of the model - they are arithmetic, and arithmetic should
not cost a token or be subject to hallucination. The model only supplies the
judgement layer (sentiment, engagement) which this module merges in.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from ..models import TranscriptSegment

FILLERS = {"um", "uh", "like", "you know", "sort of", "kind of", "basically", "actually"}


def _words(text: str) -> list[str]:
    return [w for w in text.replace("\n", " ").split(" ") if w.strip()]


def compute(
    segments: Iterable[TranscriptSegment],
    duration_s: float,
    speaker_insights: list[dict] | None = None,
) -> dict:
    segments = sorted(segments, key=lambda s: s.start_s)
    insights = {i.get("speaker"): i for i in (speaker_insights or [])}

    talk_time: dict[str, float] = defaultdict(float)
    word_count: dict[str, int] = defaultdict(int)
    questions: dict[str, int] = defaultdict(int)
    fillers: dict[str, int] = defaultdict(int)
    longest_monologue: dict[str, float] = defaultdict(float)
    turns: dict[str, int] = defaultdict(int)

    run_speaker: str | None = None
    run_len = 0.0
    switches = 0

    for seg in segments:
        dur = max(0.0, seg.end_s - seg.start_s)
        words = _words(seg.text)
        talk_time[seg.speaker] += dur
        word_count[seg.speaker] += len(words)
        questions[seg.speaker] += seg.text.count("?")
        lowered = seg.text.lower()
        fillers[seg.speaker] += sum(lowered.count(f) for f in FILLERS)

        if seg.speaker == run_speaker:
            run_len += dur
        else:
            if run_speaker is not None:
                longest_monologue[run_speaker] = max(longest_monologue[run_speaker], run_len)
                switches += 1
            run_speaker, run_len = seg.speaker, dur
            turns[seg.speaker] += 1

    if run_speaker is not None:
        longest_monologue[run_speaker] = max(longest_monologue[run_speaker], run_len)

    total_talk = sum(talk_time.values()) or 1.0
    total_words = sum(word_count.values())
    duration_s = duration_s or total_talk

    speakers = []
    for name, secs in sorted(talk_time.items(), key=lambda kv: -kv[1]):
        minutes = secs / 60 or 1e-9
        ins = insights.get(name, {})
        speakers.append(
            {
                "speaker": name,
                "talk_time_s": round(secs, 1),
                "talk_share": round(secs / total_talk, 4),
                "words": word_count[name],
                "words_per_minute": round(word_count[name] / minutes),
                "questions_asked": questions[name],
                "filler_words": fillers[name],
                "longest_monologue_s": round(longest_monologue[name], 1),
                "turns": turns[name],
                "sentiment": ins.get("sentiment", 0.0),
                "engagement": ins.get("engagement", 0.5),
                "notes": ins.get("notes", ""),
            }
        )

    # Balance: 1.0 when everyone spoke equally, 0.0 when one person spoke throughout.
    n = len(speakers) or 1
    ideal = 1 / n
    imbalance = sum(abs(s["talk_share"] - ideal) for s in speakers) / 2
    balance = round(1 - imbalance, 3) if n > 1 else 0.0

    # Interactivity: speaker switches per minute, normalised against 6/min.
    switches_per_min = switches / (duration_s / 60 or 1)
    interactivity = round(min(switches_per_min / 6, 1.0), 3)

    silence_s = max(0.0, duration_s - total_talk)

    return {
        "duration_s": round(duration_s, 1),
        "total_words": total_words,
        "talk_time_s": round(total_talk, 1),
        "silence_s": round(silence_s, 1),
        "silence_share": round(silence_s / (duration_s or 1), 3),
        "speaker_count": len(speakers),
        "speakers": speakers,
        "speaker_switches": switches,
        "balance": balance,
        "interactivity": interactivity,
        "questions_total": sum(questions.values()),
        "words_per_minute": round(total_words / (duration_s / 60 or 1)),
        "monologue_share": round(
            max((s["longest_monologue_s"] for s in speakers), default=0.0) / (duration_s or 1), 3
        ),
    }


def engagement_score(analytics: dict, sentiment: float, questions: int) -> int:
    """0-100 engagement, blending balance, interactivity, questions and mood."""
    balance = analytics.get("balance", 0.0)
    interactivity = analytics.get("interactivity", 0.0)
    duration_min = (analytics.get("duration_s", 0) or 1) / 60
    question_rate = min(questions / max(duration_min / 5, 1), 1.0)
    mood = (sentiment + 1) / 2  # -1..1 -> 0..1
    silence_penalty = min(analytics.get("silence_share", 0.0), 0.5)

    raw = (
        0.30 * balance
        + 0.30 * interactivity
        + 0.20 * question_rate
        + 0.20 * mood
        - 0.15 * silence_penalty
    )
    return int(max(0, min(100, round(raw * 100))))
