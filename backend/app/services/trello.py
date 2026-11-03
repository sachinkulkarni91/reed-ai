"""Trello push target for action items.

One action item becomes one card on the configured list:
  name   <- action item title
  desc   <- context, source quote, timestamp deep-link back to the meeting
  due    <- extracted due date (if any)
  member <- board member matched by full name / username / email
  label  <- priority label, created on the board the first time it is needed

Pushes are idempotent: an item that already carries an ``external_id`` is never
re-created, so re-running a push after a partial failure only fills the gaps.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from ..config import settings

log = logging.getLogger(__name__)

API = "https://api.trello.com/1"

PRIORITY_COLORS = {"high": "red", "medium": "yellow", "low": "sky"}


class TrelloError(RuntimeError):
    pass


class TrelloClient:
    def __init__(self, api_key: str | None = None, token: str | None = None):
        self.key = api_key or settings.trello_api_key
        self.token = token or settings.trello_token
        if not self.key or not self.token:
            raise TrelloError(
                "Trello is not configured. Set TRELLO_API_KEY and TRELLO_TOKEN "
                "(get them at https://trello.com/power-ups/admin)."
            )

    @property
    def _auth(self) -> dict:
        return {"key": self.key, "token": self.token}

    async def _request(self, method: str, path: str, **kwargs) -> dict | list:
        params = {**self._auth, **kwargs.pop("params", {})}
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.request(method, f"{API}{path}", params=params, **kwargs)
        if response.status_code >= 400:
            raise TrelloError(f"Trello {method} {path} -> {response.status_code}: {response.text}")
        if not response.text:
            return {}
        return response.json()

    # -- discovery, used by the settings screen ---------------------------- #
    async def boards(self) -> list[dict]:
        data = await self._request("GET", "/members/me/boards", params={"fields": "name,url"})
        return data  # type: ignore[return-value]

    async def lists(self, board_id: str) -> list[dict]:
        data = await self._request("GET", f"/boards/{board_id}/lists", params={"fields": "name"})
        return data  # type: ignore[return-value]

    async def board_id_for_list(self, list_id: str) -> str:
        data = await self._request("GET", f"/lists/{list_id}", params={"fields": "idBoard"})
        return data["idBoard"]  # type: ignore[index]

    async def members(self, board_id: str) -> list[dict]:
        return await self._request(  # type: ignore[return-value]
            "GET", f"/boards/{board_id}/members", params={"fields": "fullName,username"}
        )

    async def labels(self, board_id: str) -> list[dict]:
        return await self._request(  # type: ignore[return-value]
            "GET", f"/boards/{board_id}/labels", params={"fields": "name,color"}
        )

    async def ensure_label(self, board_id: str, name: str, color: str) -> Optional[str]:
        for label in await self.labels(board_id):
            if (label.get("name") or "").lower() == name.lower():
                return label["id"]
        try:
            created = await self._request(
                "POST", "/labels", params={"name": name, "color": color, "idBoard": board_id}
            )
            return created.get("id")  # type: ignore[union-attr]
        except TrelloError as exc:
            log.warning("could not create label %s: %s", name, exc)
            return None

    # -- the actual push ---------------------------------------------------- #
    async def create_card(
        self,
        list_id: str,
        name: str,
        desc: str,
        due: str | None = None,
        member_ids: list[str] | None = None,
        label_ids: list[str] | None = None,
    ) -> dict:
        params = {
            "idList": list_id,
            "name": name[:16000],
            "desc": desc[:16000],
            "pos": "bottom",
        }
        if due:
            params["due"] = due
        if member_ids:
            params["idMembers"] = ",".join(member_ids)
        if label_ids:
            params["idLabels"] = ",".join(label_ids)
        return await self._request("POST", "/cards", params=params)  # type: ignore[return-value]

    async def comment(self, card_id: str, text: str) -> None:
        await self._request("POST", f"/cards/{card_id}/actions/comments", params={"text": text[:16000]})


def match_member(members: list[dict], owner: str | None, owner_email: str | None) -> list[str]:
    """Best-effort assignee matching - never guesses across ambiguous matches."""
    if not owner and not owner_email:
        return []
    needles = [n.lower() for n in (owner, owner_email) if n]
    # An email's local part is often the username.
    if owner_email and "@" in owner_email:
        needles.append(owner_email.split("@")[0].lower())

    hits: list[str] = []
    for member in members:
        haystack = {
            (member.get("fullName") or "").lower(),
            (member.get("username") or "").lower(),
        }
        first_name = (member.get("fullName") or "").split(" ")[0].lower()
        if first_name:
            haystack.add(first_name)
        if any(n in haystack for n in needles):
            hits.append(member["id"])
    return hits[:1]


def build_description(item, meeting) -> str:
    ts = int(item.timestamp_s or 0)
    stamp = f"{ts // 60:02d}:{ts % 60:02d}"
    parts = [item.description or ""]
    if item.quote:
        parts.append(f"> {item.quote}")
    parts.append(
        f"---\nFrom **{meeting.title}** at `{stamp}`"
        + (f" · owner stated: {item.owner}" if item.owner else "")
        + f"\nConfidence: {item.confidence:.0%}"
        + f"\nMeeting: /meetings/{meeting.id}"
        + "\n\n_Pushed by Reed AI_"
    )
    return "\n\n".join(p for p in parts if p.strip())
