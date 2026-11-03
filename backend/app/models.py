"""Persistence layer.

One Meeting owns many TranscriptSegments (diarized, timestamped), one
MeetingIntelligence blob (everything Claude extracted), many ActionItems, and a
ChatMessage log for the Ask-AI copilot.
"""
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from sqlmodel import JSON, Column, Field, Relationship, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MeetingStatus(str, Enum):
    scheduled = "scheduled"
    joining = "joining"
    recording = "recording"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class Platform(str, Enum):
    google_meet = "google_meet"
    zoom = "zoom"
    teams = "teams"
    upload = "upload"


class ActionItemStatus(str, Enum):
    open = "open"
    pushed = "pushed"
    failed = "failed"
    dismissed = "dismissed"


class Meeting(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = "Untitled meeting"
    meeting_url: Optional[str] = None
    platform: Platform = Platform.upload
    status: MeetingStatus = MeetingStatus.scheduled
    error: Optional[str] = None

    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    duration_seconds: int = 0

    language: str = "en"
    audio_path: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)

    segments: List["TranscriptSegment"] = Relationship(
        back_populates="meeting",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    action_items: List["ActionItem"] = Relationship(
        back_populates="meeting",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )
    intelligence: Optional["MeetingIntelligence"] = Relationship(
        back_populates="meeting",
        sa_relationship_kwargs={"cascade": "all, delete-orphan", "uselist": False},
    )


class TranscriptSegment(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    meeting_id: int = Field(foreign_key="meeting.id", index=True)

    speaker: str = "Speaker 0"
    text: str
    start_s: float = 0.0
    end_s: float = 0.0
    confidence: float = 1.0
    is_final: bool = True
    # Sentiment is filled in post-hoc by the intelligence pass: -1.0 .. 1.0
    sentiment: Optional[float] = None

    meeting: Optional[Meeting] = Relationship(back_populates="segments")


class MeetingIntelligence(SQLModel, table=True):
    """Everything the model extracted, plus everything we computed."""

    id: Optional[int] = Field(default=None, primary_key=True)
    meeting_id: int = Field(foreign_key="meeting.id", index=True, unique=True)

    headline: str = ""
    summary: str = ""
    detailed_notes: str = ""
    recap_email: str = ""

    key_points: List = Field(default_factory=list, sa_column=Column(JSON))
    decisions: List = Field(default_factory=list, sa_column=Column(JSON))
    key_questions: List = Field(default_factory=list, sa_column=Column(JSON))
    risks: List = Field(default_factory=list, sa_column=Column(JSON))
    topics: List = Field(default_factory=list, sa_column=Column(JSON))
    chapters: List = Field(default_factory=list, sa_column=Column(JSON))
    highlights: List = Field(default_factory=list, sa_column=Column(JSON))

    # Computed analytics (talk time, engagement, sentiment timeline, scores)
    analytics: Dict = Field(default_factory=dict, sa_column=Column(JSON))

    model_used: str = ""
    generated_at: datetime = Field(default_factory=utcnow)

    meeting: Optional[Meeting] = Relationship(back_populates="intelligence")


class ActionItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    meeting_id: int = Field(foreign_key="meeting.id", index=True)

    title: str
    description: str = ""
    owner: Optional[str] = None
    owner_email: Optional[str] = None
    due_date: Optional[str] = None  # ISO date, model-extracted
    priority: str = "medium"  # low | medium | high
    quote: str = ""
    timestamp_s: float = 0.0
    confidence: float = 0.0

    status: ActionItemStatus = ActionItemStatus.open
    external_id: Optional[str] = None      # Trello card id
    external_url: Optional[str] = None     # Trello short url
    push_error: Optional[str] = None
    pushed_at: Optional[datetime] = None

    created_at: datetime = Field(default_factory=utcnow)

    meeting: Optional[Meeting] = Relationship(back_populates="action_items")


class SpeakerProfile(SQLModel, table=True):
    """A person's voice fingerprint, refined across meetings.

    The embedding is a unit-normalized wespeaker vector (same model the
    diarizer uses), maintained as a capped running average so it adapts to
    mic and voice changes without being hijacked by one bad recording.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    embedding: List = Field(default_factory=list, sa_column=Column(JSON))
    samples: int = 1
    updated_at: datetime = Field(default_factory=utcnow)


class ChatMessage(SQLModel, table=True):
    """Ask-AI copilot history, scoped to a meeting (or global when null)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    meeting_id: Optional[int] = Field(default=None, foreign_key="meeting.id", index=True)
    role: str  # user | assistant
    content: str
    created_at: datetime = Field(default_factory=utcnow)
