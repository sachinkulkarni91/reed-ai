"""Voice fingerprint registry - staged copy.

The apply script moves this to app/services/voiceprints.py; it lives outside
app/ until then so the --reload server doesn't pick up a half-wired feature.

How it works
------------
Enrollment: after a meeting's speakers are named (screen tiles, conversation,
or a manual rename), each named speaker's longest turns are run through the
same wespeaker embedding model pyannote diarization already uses, averaged,
and upserted into a SpeakerProfile row - a running average across meetings,
so profiles improve with every appearance.

Recognition: on the next meeting, each diarized cluster is embedded the same
way and cosine-compared against the registry. A match needs both a floor
(>= 0.55) and daylight over the runner-up (>= 0.08) - a 3-second utterance
from a known voice names instantly; an unknown voice stays unnamed rather
than being guessed.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, select

from ..db import engine
from ..models import SpeakerProfile

log = logging.getLogger(__name__)

SIM_ACCEPT = 0.55       # cosine floor for a match
SIM_MARGIN = 0.08       # best must beat runner-up by this much
MIN_TURN_S = 2.0        # shortest turn worth embedding
TURNS_PER_SPEAKER = 3   # longest N turns averaged per speaker
MAX_PROFILE_WEIGHT = 10  # cap so profiles stay adaptive to voice/mic changes

_embedder = None


def _get_embedder():
    global _embedder
    if _embedder is None:
        import os

        from pyannote.audio import Inference, Model

        from ..config import settings

        token = settings.hf_token or os.environ.get("HF_TOKEN")
        model = Model.from_pretrained("pyannote/wespeaker-voxceleb-resnet34-LM", token=token)
        _embedder = Inference(model, window="whole")
    return _embedder


def _embed_speaker(wav_path: Path, turns: list[tuple[float, float]]):
    """Average embedding over a speaker's turns; None if nothing usable."""
    import numpy as np
    from pyannote.core import Segment as PSegment

    from .transcription.whisper_file import _load_waveform

    audio = _load_waveform(wav_path)
    total_s = audio["waveform"].shape[1] / audio["sample_rate"]
    embedder = _get_embedder()
    vectors = []
    for start, end in turns:
        # pyannote's crop rejects end == file duration (float-precision strict
        # check), so keep a hair inside the waveform.
        start, end = max(0.0, start), min(end, total_s - 0.05)
        if end - start < MIN_TURN_S:
            continue
        try:
            vec = embedder.crop(audio, PSegment(start, end))
            vectors.append(np.asarray(vec).reshape(-1))
        except Exception as exc:
            log.debug("embedding crop failed %.1f-%.1f: %s", start, end, exc)
    if not vectors:
        return None
    mean = np.mean(vectors, axis=0)
    return mean / (np.linalg.norm(mean) + 1e-9)


def _longest_turns(views, speaker: str) -> list[tuple[float, float]]:
    turns = sorted(
        ((v.start_s, v.end_s) for v in views if v.speaker == speaker),
        key=lambda t: t[0] - t[1],  # longest first
    )
    return turns[:TURNS_PER_SPEAKER]


def _match_registry_sync(wav_path: Path, views, clusters: list[str]) -> dict[str, str]:
    import numpy as np

    with Session(engine) as session:
        profiles = session.exec(select(SpeakerProfile)).all()
    if not profiles:
        return {}
    names = [p.name for p in profiles]
    matrix = np.array([p.embedding for p in profiles])  # already normalized

    mapping: dict[str, str] = {}
    for cluster in clusters:
        emb = _embed_speaker(wav_path, _longest_turns(views, cluster))
        if emb is None:
            continue
        sims = matrix @ emb
        order = np.argsort(sims)[::-1]
        best, second = sims[order[0]], (sims[order[1]] if len(sims) > 1 else -1.0)
        if best >= SIM_ACCEPT and best - second >= SIM_MARGIN:
            mapping[cluster] = names[int(order[0])]
            log.info("voiceprint: %s -> %s (sim=%.2f, margin=%.2f)",
                     cluster, names[int(order[0])], best, best - second)
    # one voice, one name
    seen: set[str] = set()
    return {c: n for c, n in mapping.items() if not (n in seen or seen.add(n))}


async def match_registry(wav_path, views, clusters: list[str]) -> dict[str, str]:
    """{cluster label -> known name} for voices already in the registry."""
    try:
        return await asyncio.to_thread(_match_registry_sync, Path(wav_path), views, clusters)
    except Exception:
        log.exception("voiceprint matching failed")
        return {}


def _update_registry_sync(wav_path: Path, views, named_speakers: list[str]) -> int:
    import numpy as np

    updated = 0
    with Session(engine) as session:
        for name in named_speakers:
            emb = _embed_speaker(wav_path, _longest_turns(views, name))
            if emb is None:
                continue
            profile = session.exec(
                select(SpeakerProfile).where(SpeakerProfile.name == name)
            ).first()
            if profile is None:
                profile = SpeakerProfile(name=name, embedding=emb.tolist(), samples=1)
            else:
                weight = min(profile.samples, MAX_PROFILE_WEIGHT)
                merged = np.array(profile.embedding) * weight + emb
                merged = merged / (np.linalg.norm(merged) + 1e-9)
                profile.embedding = merged.tolist()
                profile.samples += 1
            profile.updated_at = datetime.now(timezone.utc)
            session.add(profile)
            updated += 1
        session.commit()
    return updated


async def update_registry(wav_path, views, named_speakers: list[str]) -> None:
    """Enroll/refresh profiles for this meeting's named speakers."""
    real = [n for n in named_speakers if not n.startswith("Speaker ")]
    if not real:
        return
    try:
        count = await asyncio.to_thread(_update_registry_sync, Path(wav_path), views, real)
        log.info("voiceprint registry updated for %d speaker(s)", count)
    except Exception:
        log.exception("voiceprint enrollment failed")
