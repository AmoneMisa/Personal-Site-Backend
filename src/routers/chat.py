import json
import re
from typing import Optional

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.session import SessionLocal
from ..services.ws_manager import ws_manager
from ..utils.redis_client import get_redis
from ..models.telegramModels import ChatSession, ChatMessage, SessionStatus

router = APIRouter(prefix="/chat", tags=["chat"])

TG_USERNAME_RE = re.compile(r"^@[A-Za-z0-9_]{5,32}$")


def _client_id(request: Request) -> str:
    return (request.headers.get("X-Client-Id") or "").strip()


def _ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


def _session_is_open(status: SessionStatus) -> bool:
    return status in (SessionStatus.active, SessionStatus.awaiting_username)


async def _get_latest_session(db: AsyncSession, client_id: str) -> Optional[ChatSession]:
    q = (
        select(ChatSession)
        .where(ChatSession.client_id == client_id)
        .order_by(desc(ChatSession.id))
        .limit(1)
    )
    return (await db.execute(q)).scalars().first()


async def _get_or_create_open_session(db: AsyncSession, client_id: str, ip: Optional[str]) -> ChatSession:
    latest = await _get_latest_session(db, client_id)
    if latest and _session_is_open(latest.status):
        return latest

    s = ChatSession(client_id=client_id, ip=ip, status=SessionStatus.active)
    db.add(s)
    await db.commit()
    await db.refresh(s)

    return s


@router.post("/start")
async def start(request: Request):
    client_id = _client_id(request)
    if not client_id:
        return {"ok": False, "error": "missing_client_id"}

    ip = _ip(request)

    async with SessionLocal() as db:
        s = await _get_or_create_open_session(db, client_id, ip)
        return {"ok": True, "session": {"id": s.id, "status": s.status, "tgUsername": s.tg_username}}


@router.get("/history")
async def history(request: Request):
    client_id = _client_id(request)
    if not client_id:
        return {"ok": False, "error": "missing_client_id"}

    async with SessionLocal() as db:
        s = await _get_latest_session(db, client_id)

        if not s:
            return {"ok": True, "session": None, "messages": []}

        mq = (
            select(ChatMessage)
            .where(ChatMessage.session_id == s.id)
            .order_by(ChatMessage.id.asc())
            .limit(300)
        )
        msgs = (await db.execute(mq)).scalars().all()

        return {
            "ok": True,
            "session": {"id": s.id, "status": s.status, "tgUsername": s.tg_username},
            "messages": [
                {"id": m.id, "sender": m.sender, "text": m.text, "createdAt": m.created_at.isoformat()}
                for m in msgs
            ],
        }


@router.post("/send")
async def send(payload: dict, request: Request):
    client_id = _client_id(request)
    if not client_id:
        return {"ok": False, "error": "missing_client_id"}

    text = (payload.get("text") or "").strip()
    client_msg_id = (payload.get("clientMsgId") or "").strip() or None

    if not text:
        return {"ok": False, "error": "empty"}

    ip = _ip(request)

    async with SessionLocal() as db:
        s = await _get_or_create_open_session(db, client_id, ip)
        if s.status == SessionStatus.closed:
            return {"ok": False, "error": "closed"}

        if s.status != SessionStatus.active:
            s.status = SessionStatus.active
            await db.commit()

        m = ChatMessage(session_id=s.id, sender="client", text=text)
        db.add(m)
        await db.commit()
        await db.refresh(m)

    ws_payload = {
        "type": "message",
        "id": m.id,
        "sessionId": s.id,
        "sender": "client",
        "text": text,
        "createdAt": m.created_at.isoformat(),
        "clientMsgId": client_msg_id,
    }
    await ws_manager.send(client_id, ws_payload)

    try:
        r = get_redis()
        await r.publish(
            "tg.notify",
            json.dumps(
                {
                    "type": "chat_message",
                    "source": "site",
                    "sessionId": s.id,
                    "clientId": client_id,
                    "text": text,
                    "createdAt": m.created_at.isoformat(),
                },
                ensure_ascii=False,
            ),
        )
    except Exception:
        pass

    return {"ok": True, "sessionId": s.id}


@router.post("/set-channel")
async def set_channel(payload: dict, request: Request):
    client_id = _client_id(request)
    if not client_id:
        return {"ok": False, "error": "missing_client_id"}

    channel = payload.get("channel")
    tg_username = (payload.get("tgUsername") or "").strip()
    ip = _ip(request)

    async with SessionLocal() as db:
        s = await _get_or_create_open_session(db, client_id, ip)
        if s.status == SessionStatus.closed:
            return {"ok": False, "error": "closed"}

        if channel == "telegram":
            if not TG_USERNAME_RE.match(tg_username):
                return {"ok": False, "error": "invalid_username"}

            s.tg_username = tg_username
            s.status = SessionStatus.moved_to_telegram
            s.closed_at = func.now()
            await db.commit()
            await db.refresh(s)

            try:
                r = get_redis()
                await r.publish(
                    "tg.notify",
                    json.dumps(
                        {
                            "type": "chat_message",
                            "source": "telegram",
                            "sessionId": s.id,
                            "clientId": client_id,
                            "tgUsername": tg_username,
                            "text": "",
                        },
                        ensure_ascii=False,
                    ),
                )
            except Exception:
                pass

            await ws_manager.send(
                client_id,
                {
                    "type": "session_closed",
                    "sessionId": s.id,
                    "reason": "moved_to_telegram",
                    "session": {"id": s.id, "status": s.status, "tgUsername": s.tg_username},
                },
            )

            return {"ok": True, "session": {"id": s.id, "status": s.status, "tgUsername": s.tg_username}}

        if channel == "site":
            if s.status != SessionStatus.closed:
                s.status = SessionStatus.active
                await db.commit()
                await db.refresh(s)

            await ws_manager.send(
                client_id,
                {"type": "session_update", "session": {"id": s.id, "status": s.status, "tgUsername": s.tg_username}},
            )
            return {"ok": True, "session": {"id": s.id, "status": s.status, "tgUsername": s.tg_username}}

        return {"ok": False, "error": "invalid_channel"}


@router.post("/close")
async def close(payload: dict, request: Request):
    client_id = _client_id(request)
    if not client_id:
        return {"ok": False, "error": "missing_client_id"}

    reason = (payload.get("reason") or "closed_by_client").strip()

    async with SessionLocal() as db:
        s = await _get_latest_session(db, client_id)
        if not s:
            return {"ok": True, "session": None}

        if s.status != SessionStatus.closed:
            s.status = SessionStatus.closed
            s.closed_at = func.now()
            await db.commit()
            await db.refresh(s)

    await ws_manager.send(
        client_id,
        {
            "type": "session_closed",
            "sessionId": s.id,
            "reason": reason,
            "session": {"id": s.id, "status": s.status, "tgUsername": s.tg_username},
        },
    )

    try:
        r = get_redis()
        await r.publish(
            "tg.notify",
            json.dumps(
                {"type": "session_closed", "source": "site", "sessionId": s.id, "clientId": client_id, "reason": reason},
                ensure_ascii=False,
            ),
        )
    except Exception:
        pass

    return {"ok": True, "session": {"id": s.id, "status": s.status, "tgUsername": s.tg_username}}


@router.post("/owner-reply")
async def owner_reply(payload: dict, request: Request):
    session_id = int(payload.get("sessionId") or 0)
    text = (payload.get("text") or "").strip()
    if not session_id or not text:
        return {"ok": False, "error": "empty"}

    async with SessionLocal() as db:
        s = await db.get(ChatSession, session_id)
        if not s:
            return {"ok": False, "error": "not_found"}

        if s.status == SessionStatus.closed:
            return {"ok": False, "error": "closed"}

        m = ChatMessage(session_id=s.id, sender="owner", text=text)
        db.add(m)
        await db.commit()
        await db.refresh(m)

        client_id = s.client_id

    await ws_manager.send(
        client_id,
        {
            "type": "message",
            "id": m.id,
            "sessionId": session_id,
            "sender": "owner",
            "text": text,
            "createdAt": m.created_at.isoformat(),
        },
    )

    return {"ok": True}


@router.websocket("/ws")
async def ws_chat(ws: WebSocket, clientId: str):
    await ws_manager.connect(clientId, ws)
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    finally:
        await ws_manager.disconnect(clientId, ws)