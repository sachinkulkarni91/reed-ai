"""Claude Opus 5 intelligence layer.

Two entry points:
  * ``extract_intelligence`` - one structured-output pass over the whole
    transcript that produces the summary, chapters, decisions, questions,
    highlights and action items.
  * ``stream_chat`` - the Ask-AI copilot, streamed token by token.

Both build the *same* cached system prefix (instructions + transcript) so that
follow-up copilot turns on a long meeting read from cache instead of re-paying
for the transcript on every question.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Optional

import anthropic
from pydantic import BaseModel, Field

from ..config import settings

log = logging.getLogger(__name__)

BASE_SYSTEM = (
    "You are Reed AI, a meeting intelligence engine. You read diarized meeting "
    "transcripts and produce precise, sourced, non-speculative output.\n"
    "Rules:\n"
    "- Never invent commitments. An action item exists only if someone actually "
    "committed to doing something, or was asked to and did not decline.\n"
    "- Attribute owners using the exact speaker label from the transcript, or the "
    "person's name if they are named in the conversation.\n"
    "- Every timestamp you emit must come from the segment markers in the transcript.\n"
    "- Resolve relative dates against the meeting date given to you and emit ISO "
    "YYYY-MM-DD. If a due date was never stated, leave it null rather than guessing.\n"
    "- Quote verbatim when asked for a quote."
)


# --------------------------------------------------------------------------- #
# Structured output schema
# --------------------------------------------------------------------------- #
class ExtractedActionItem(BaseModel):
    title: str
    description: str = ""
    owner: Optional[str] = None
    owner_email: Optional[str] = None
    due_date: Optional[str] = None
    priority: str = "medium"
    quote: str = ""
    timestamp_s: float = 0.0
    confidence: float = 0.5


class Decision(BaseModel):
    decision: str
    context: str = ""
    timestamp_s: float = 0.0


class KeyQuestion(BaseModel):
    question: str
    asked_by: Optional[str] = None
    answered: bool = False
    answer: str = ""


class Risk(BaseModel):
    risk: str
    severity: str = "medium"


class Topic(BaseModel):
    name: str
    weight: float = 0.0
    mentions: int = 0


class Chapter(BaseModel):
    title: str
    start_s: float = 0.0
    end_s: float = 0.0
    summary: str = ""


class Highlight(BaseModel):
    quote: str
    speaker: str = ""
    timestamp_s: float = 0.0
    why: str = ""


class SpeakerInsight(BaseModel):
    speaker: str
    sentiment: float = 0.0
    engagement: float = 0.5
    notes: str = ""


class SpeakerName(BaseModel):
    label: str = Field(description="The diarized label exactly as it appears, e.g. 'Speaker 0'.")
    name: str = Field(description="The person's real name as evidenced in the conversation.")
    confidence: float = Field(default=0.5, description="0-1. Only high when the evidence is direct.")


class SentimentPoint(BaseModel):
    timestamp_s: float
    sentiment: float
    label: str = ""


class MeetingNarrative(BaseModel):
    """The prose half of the extraction - one structured-output call."""

    headline: str
    summary: str
    detailed_notes: str
    recap_email: str
    key_points: list[str] = Field(default_factory=list)
    topics: list[Topic] = Field(default_factory=list)
    highlights: list[Highlight] = Field(default_factory=list)


class MeetingAnalysis(BaseModel):
    """The structured/analytical half of the extraction - the other call.

    Split from ``MeetingNarrative`` because one schema covering every field
    compiles into a strict-output grammar too large for the API to accept
    (``invalid_request_error: compiled grammar is too large``). Two calls
    against the same cached transcript prefix cost the same in cache-read
    tokens as one; only the (cheap) instruction text is duplicated.
    """

    decisions: list[Decision] = Field(default_factory=list)
    key_questions: list[KeyQuestion] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    chapters: list[Chapter] = Field(default_factory=list)
    action_items: list[ExtractedActionItem] = Field(default_factory=list)
    speaker_insights: list[SpeakerInsight] = Field(default_factory=list)
    speaker_names: list[SpeakerName] = Field(default_factory=list)
    sentiment_timeline: list[SentimentPoint] = Field(default_factory=list)
    overall_sentiment: float = 0.0
    meeting_score: int = 0
    score_rationale: str = ""


class MeetingExtraction(MeetingNarrative, MeetingAnalysis):
    """Merged result handed to the rest of the app - same shape as before the split."""


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
def _client() -> anthropic.AsyncAnthropic:
    if not settings.anthropic_api_key:
        # Falls back to ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / `ant auth login`.
        return anthropic.AsyncAnthropic()
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


def _cached_system(transcript: str, meta: str) -> list[dict]:
    """Stable prefix -> cache hit on every follow-up question about this meeting."""
    return [
        {"type": "text", "text": BASE_SYSTEM},
        {
            "type": "text",
            "text": f"# Meeting context\n{meta}\n\n# Transcript\n{transcript}",
            "cache_control": {"type": "ephemeral"},
        },
    ]


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
NARRATIVE_PROMPT = (
    "Write up the meeting above: headline, summary, detailed notes, recap email, "
    "key points, topics and highlights.\n\n"
    "For topics: weight is the 0-1 share of meeting time spent on it.\n"
    "For highlights: pick moments worth quoting back, with why they matter.\n"
    "Keep the output bounded: at most 10 key points, 8 topics, 6 highlights; "
    "detailed_notes under 600 words; recap_email under 250 words."
)

ANALYSIS_PROMPT = (
    "Analyse the meeting above: decisions, key questions, risks, chapters, action "
    "items, speaker insights, sentiment timeline, and an effectiveness score.\n\n"
    "For chapters: segment the meeting into 3-8 contiguous chapters that cover the "
    "full duration, with real start/end timestamps.\n"
    "For sentiment_timeline: emit one point every few minutes so the curve reflects "
    "how the room actually moved.\n"
    "For action_items: only real commitments. Prefer fewer, correct items over "
    "exhaustive guesses; set confidence honestly.\n"
    "For meeting_score: judge whether this meeting reached decisions and clear "
    "ownership, not whether people were pleasant.\n"
    "For speaker_names: infer who each diarized 'Speaker N' actually is from the "
    "conversation itself - people addressing each other by name right before a "
    "reply, self-introductions, or being asked to do something and accepting. "
    "Only include a mapping when the evidence is specific; set confidence "
    "honestly and omit speakers you cannot identify. Never guess from job roles "
    "alone.\n"
    "Keep the output bounded - pick the most important items only: at most 10 "
    "decisions, 10 key questions, 8 risks, 8 chapters, 12 action items, 8 "
    "speaker insights, and 12 sentiment points. Keep quotes and context fields "
    "short (one sentence)."
)


async def _parse(system: list[dict], prompt: str, schema: type[BaseModel], max_tokens: int, label: str):
    from pydantic import ValidationError

    client = _client()
    try:
        response = await client.messages.parse(
            model=settings.reed_model,
            max_tokens=max_tokens,
            thinking={"type": "adaptive"},
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
        )
    except ValidationError:
        # Truncated JSON: the response hit max_tokens mid-structure (long
        # meetings, token-dense scripts like Devanagari). One retry with a
        # bigger budget; the bounded-output prompt keeps this rare.
        log.warning("%s output truncated at %d tokens - retrying with a larger budget", label, max_tokens)
        response = await client.messages.parse(
            model=settings.reed_model,
            max_tokens=min(max_tokens * 2, 20_000),
            thinking={"type": "adaptive"},
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
        )
    usage = getattr(response, "usage", None)
    if usage is not None:
        log.info(
            "%s usage: in=%s cache_read=%s out=%s",
            label,
            getattr(usage, "input_tokens", "?"),
            getattr(usage, "cache_read_input_tokens", "?"),
            getattr(usage, "output_tokens", "?"),
        )
    return response.parsed_output


async def extract_intelligence(transcript: str, meta: str) -> MeetingExtraction:
    """Two structured-output calls in parallel, both reading the same cached transcript.

    One call covering every field compiles into a strict-output grammar too large
    for the API (``compiled grammar is too large``) - splitting narrative fields
    from analytical fields keeps each call's schema small enough to compile.
    """
    system = _cached_system(transcript, meta)
    narrative, analysis = await asyncio.gather(
        _parse(system, NARRATIVE_PROMPT, MeetingNarrative, 8000, "narrative"),
        _parse(system, ANALYSIS_PROMPT, MeetingAnalysis, 12000, "analysis"),
    )
    return MeetingExtraction(**narrative.model_dump(), **analysis.model_dump())


# --------------------------------------------------------------------------- #
# Ask-AI copilot
# --------------------------------------------------------------------------- #
async def stream_chat(
    question: str,
    transcript: str,
    meta: str,
    history: list[dict] | None = None,
) -> AsyncIterator[str]:
    client = _client()
    messages = list(history or [])
    messages.append({"role": "user", "content": question})

    async with client.messages.stream(
        model=settings.reed_model,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        system=_cached_system(transcript, meta)
        + [
            {
                "type": "text",
                "text": (
                    "Answer questions about this meeting. Cite timestamps like [12:04] "
                    "when you reference something specific. If the transcript does not "
                    "contain the answer, say so plainly instead of speculating."
                ),
            }
        ],
        messages=messages,
    ) as stream:
        async for text in stream.text_stream:
            yield text


async def stream_search(question: str, corpus: str) -> AsyncIterator[str]:
    """Cross-meeting search copilot over a condensed corpus of past meetings."""
    client = _client()
    async with client.messages.stream(
        model=settings.reed_model,
        max_tokens=4000,
        thinking={"type": "adaptive"},
        system=[
            {"type": "text", "text": BASE_SYSTEM},
            {
                "type": "text",
                "text": f"# Meeting library\n{corpus}",
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": (
                    "Answer across meetings. Always name the meeting and date you are "
                    "drawing from. Say when the library does not cover the question."
                ),
            },
        ],
        messages=[{"role": "user", "content": question}],
    ) as stream:
        async for text in stream.text_stream:
            yield text
