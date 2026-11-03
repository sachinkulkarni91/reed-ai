"""Audio ingest socket.

The capture script inside the meeting tab connects here and pushes WebM/Opus
chunks roughly four times a second. Binary frames are audio; the one text frame
we accept is ``{"type":"eos"}``, sent when the recorder stops.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlmodel import Session

from ..bot.manager import registry
from ..db import engine
from ..models import Meeting

log = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/ingest/{meeting_id}")
async def ingest(websocket: WebSocket, meeting_id: int):
    await websocket.accept()

    with Session(engine) as session:
        if session.get(Meeting, meeting_id) is None:
            await websocket.close(code=4404, reason="unknown meeting")
            return

    live = registry.get_or_create_session(meeting_id)
    try:
        await live.start()
    except Exception as exc:
        log.exception("could not start transcription for meeting %s", meeting_id)
        await websocket.close(code=4500, reason=str(exc)[:100])
        return

    log.info("ingest socket open for meeting %s", meeting_id)
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if (chunk := message.get("bytes")) is not None:
                await live.feed(chunk)
            elif (text := message.get("text")) is not None:
                try:
                    if json.loads(text).get("type") == "eos":
                        break
                except ValueError:
                    continue
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ingest socket error for meeting %s", meeting_id)
    finally:
        log.info("ingest socket closed for meeting %s (%s bytes)", meeting_id, live.bytes_in)
