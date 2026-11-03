from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..db import get_session
from ..models import ActionItem, ActionItemStatus, Meeting
from ..services.pipeline import push_action_items
from ..services.trello import TrelloError

router = APIRouter(prefix="/api/action-items", tags=["action-items"])


class ActionItemUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    owner: str | None = None
    owner_email: str | None = None
    due_date: str | None = None
    priority: str | None = None
    status: ActionItemStatus | None = None


class ActionItemCreate(BaseModel):
    meeting_id: int
    title: str
    description: str = ""
    owner: str | None = None
    owner_email: str | None = None
    due_date: str | None = None
    priority: str = "medium"


class PushRequest(BaseModel):
    meeting_id: int
    item_ids: list[int] | None = None
    list_id: str | None = None


@router.get("")
def list_action_items(
    meeting_id: int | None = None,
    status: ActionItemStatus | None = None,
    session: Session = Depends(get_session),
):
    query = select(ActionItem)
    if meeting_id is not None:
        query = query.where(ActionItem.meeting_id == meeting_id)
    if status is not None:
        query = query.where(ActionItem.status == status)
    items = session.exec(query.order_by(ActionItem.created_at.desc())).all()

    titles = {m.id: m.title for m in session.exec(select(Meeting)).all()}
    return [{**item.model_dump(), "meeting_title": titles.get(item.meeting_id, "")} for item in items]


@router.post("", status_code=201)
def create_action_item(payload: ActionItemCreate, session: Session = Depends(get_session)):
    if session.get(Meeting, payload.meeting_id) is None:
        raise HTTPException(404, "Meeting not found")
    item = ActionItem(**payload.model_dump(), confidence=1.0)
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


@router.patch("/{item_id}")
def update_action_item(
    item_id: int, payload: ActionItemUpdate, session: Session = Depends(get_session)
):
    item = session.get(ActionItem, item_id)
    if item is None:
        raise HTTPException(404, "Action item not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, field, value)
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


@router.delete("/{item_id}", status_code=204)
def delete_action_item(item_id: int, session: Session = Depends(get_session)):
    item = session.get(ActionItem, item_id)
    if item is None:
        raise HTTPException(404, "Action item not found")
    session.delete(item)
    session.commit()


@router.post("/push")
async def push(payload: PushRequest):
    """Push action items to Trello. Already-pushed items are skipped, not duplicated."""
    try:
        return await push_action_items(payload.meeting_id, payload.item_ids, payload.list_id)
    except TrelloError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
