"""Auto-join: watch the bot account's calendar and join Teams invites on time.

The bot account (an Outlook.com mailbox) receives meeting invites like any
attendee. This module reads its calendar through Microsoft Graph and, when a
Teams meeting is about to start, launches the same bot the "Send bot" button
does. Auth is a delegated device-code sign-in made once by a person
(scripts/calendar_login.py); only a refresh token is stored.

Graph app registration needs: personal Microsoft accounts supported, "Allow
public client flows" on, delegated permission Calendars.Read.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import httpx
from sqlmodel import Session, select

from ..config import settings
from ..db import engine
from ..models import Meeting

log = logging.getLogger(__name__)

AUTHORITY = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPE = "offline_access Calendars.Read"
LEAD_SECONDS = 90  # join this long before the scheduled start
TEAMS_URL = re.compile(
    r"https://teams\.(?:live|microsoft)\.com/(?:meet/|l/meetup-join/)[^\s\"'<>\\]+", re.I
)


# -- token handling -------------------------------------------------------- #
def _token_path() -> Path:
    return Path(settings.calendar_token_file)


def device_login() -> None:
    """Interactive one-time sign-in (run from scripts/calendar_login.py)."""
    if not settings.ms_client_id:
        raise SystemExit("MS_CLIENT_ID is not set (see .env.example)")
    with httpx.Client(timeout=30) as client:
        r = client.post(
            f"{AUTHORITY}/devicecode",
            data={"client_id": settings.ms_client_id, "scope": SCOPE},
        )
        r.raise_for_status()
        dc = r.json()
        print(dc["message"])
        deadline = time.time() + dc["expires_in"]
        while time.time() < deadline:
            time.sleep(dc.get("interval", 5))
            t = client.post(
                f"{AUTHORITY}/token",
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": settings.ms_client_id,
                    "device_code": dc["device_code"],
                },
            )
            body = t.json()
            if t.status_code == 200:
                _save_token(body)
                print(f"Signed in. Calendar access saved to {_token_path()}")
                return
            if body.get("error") != "authorization_pending":
                raise SystemExit(f"Sign-in failed: {body.get('error_description', body)}")
    raise SystemExit("Timed out waiting for sign-in.")


def _save_token(body: dict) -> None:
    path = _token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"refresh_token": body["refresh_token"]}), encoding="utf-8")


def _load_refresh_token() -> str | None:
    env = os.environ.get("GRAPH_REFRESH_TOKEN")
    if env:
        return env
    path = _token_path()
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("refresh_token")
        except Exception:
            return None
    return None


async def _access_token(client: httpx.AsyncClient) -> str | None:
    refresh = _load_refresh_token()
    if not refresh:
        return None
    r = await client.post(
        f"{AUTHORITY}/token",
        data={
            "grant_type": "refresh_token",
            "client_id": settings.ms_client_id,
            "refresh_token": refresh,
            "scope": SCOPE,
        },
    )
    if r.status_code != 200:
        log.warning("calendar token refresh failed: %s", r.text[:200])
        return None
    body = r.json()
    if body.get("refresh_token") and not os.environ.get("GRAPH_REFRESH_TOKEN"):
        _save_token(body)  # refresh tokens rotate
    return body["access_token"]


# -- calendar --------------------------------------------------------------- #
def teams_url_from_event(event: dict) -> str | None:
    joined = (event.get("onlineMeeting") or {}).get("joinUrl")
    if joined:
        return joined
    haystack = " ".join(
        [
            (event.get("body") or {}).get("content") or "",
            (event.get("location") or {}).get("displayName") or "",
            event.get("bodyPreview") or "",
        ]
    )
    haystack = unquote(html.unescape(haystack))
    m = TEAMS_URL.search(haystack)
    return m.group(0).rstrip(".,)") if m else None


def _parse_utc(value: dict) -> datetime:
    raw = value["dateTime"].split(".")[0]
    return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)


def _joined_path() -> Path:
    return _token_path().with_name("auto-joined.json")


def _load_joined() -> set[str]:
    try:
        return set(json.loads(_joined_path().read_text(encoding="utf-8")))
    except Exception:
        return set()


def _remember_joined(ids: set[str]) -> None:
    try:
        _joined_path().write_text(json.dumps(sorted(ids)[-500:]), encoding="utf-8")
    except Exception:
        log.exception("could not persist auto-joined event ids")


def _already_running(url: str) -> bool:
    cutoff = datetime.utcnow() - timedelta(hours=6)
    with Session(engine) as session:
        return (
            session.exec(
                select(Meeting).where(Meeting.meeting_url == url, Meeting.created_at > cutoff)
            ).first()
            is not None
        )


async def _poll_once(client: httpx.AsyncClient, joined: set[str]) -> None:
    token = await _access_token(client)
    if not token:
        return
    now = datetime.now(timezone.utc)
    r = await client.get(
        f"{GRAPH}/me/calendarView",
        params={
            "startDateTime": (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endDateTime": (now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "$select": "id,subject,start,end,isCancelled,onlineMeeting,body,bodyPreview,location",
            "$top": "25",
        },
        headers={"Authorization": f"Bearer {token}", "Prefer": 'outlook.timezone="UTC"'},
    )
    if r.status_code != 200:
        log.warning("calendar read failed: %s %s", r.status_code, r.text[:200])
        return

    from ..api.bot import launch_bot

    for event in r.json().get("value", []):
        eid = event.get("id")
        if not eid or eid in joined or event.get("isCancelled"):
            continue
        start, end = _parse_utc(event["start"]), _parse_utc(event["end"])
        if not (start - timedelta(seconds=LEAD_SECONDS) <= now < end):
            continue
        url = teams_url_from_event(event)
        if not url:
            continue
        joined.add(eid)
        _remember_joined(joined)
        if _already_running(url):
            log.info("calendar: %r already joined, skipping", event.get("subject"))
            continue
        port = os.environ.get("PORT") or settings.server_port
        log.info("calendar: auto-joining %r (starts %s)", event.get("subject"), start.isoformat())
        with Session(engine) as session:
            launch_bot(
                session,
                url,
                lambda mid: f"ws://127.0.0.1:{port}/ws/ingest/{mid}",
                title=event.get("subject") or "Live meeting",
            )


async def run_forever() -> None:
    if not settings.auto_join_enabled:
        return
    if not settings.ms_client_id or not _load_refresh_token():
        log.warning(
            "AUTO_JOIN_ENABLED but calendar is not set up (need MS_CLIENT_ID and a "
            "one-time scripts/calendar_login.py sign-in) - auto-join is off"
        )
        return
    log.info("calendar auto-join watching every %ss", settings.calendar_poll_seconds)
    joined = _load_joined()
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                await _poll_once(client, joined)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("calendar poll failed")
            await asyncio.sleep(settings.calendar_poll_seconds)
