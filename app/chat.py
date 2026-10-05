from datetime import datetime, timezone
import os
import re
import secrets

import socketio
from bson import ObjectId
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from motor.motor_asyncio import AsyncIOMotorDatabase
from jose import JWTError, jwt

from .ai import chat_reply
from .core import get_current_user, get_database, json_safe, object_id, settings


router = APIRouter(prefix="/api/chat", tags=["chat"], dependencies=[Depends(get_current_user)])
socket_server = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins=["http://localhost:5173", "http://127.0.0.1:5173"], logger=False, engineio_logger=False)
CHAT_UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads", "chat")
os.makedirs(CHAT_UPLOAD_DIR, exist_ok=True)
ALLOWED_FILES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp", "application/pdf", "application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "text/plain"}


async def _can_chat(database: AsyncIOMotorDatabase, user: dict, contact_id: ObjectId) -> bool:
    role = user.get("role")
    if role not in {"caregiver", "expert"}:
        return False
    caregiver_id = user["_id"] if role == "caregiver" else contact_id
    expert_id = contact_id if role == "caregiver" else user["_id"]
    caregiver = await database.users.find_one({"_id": caregiver_id, "role": "caregiver", "expertRequests": {"$elemMatch": {"expertId": expert_id, "status": "approved"}}}, {"_id": 1})
    return caregiver is not None


@router.get("/contacts")
async def get_contacts(user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    if user.get("role") == "caregiver":
        contacts = []
        for request in user.get("expertRequests", []):
            if request.get("status") == "approved":
                contact = await database.users.find_one({"_id": request.get("expertId")}, {"name": 1, "email": 1, "role": 1})
                if contact:
                    contacts.append(contact)
    elif user.get("role") == "expert":
        caregivers = await database.users.find({"role": "caregiver", "expertRequests": {"$elemMatch": {"expertId": user["_id"], "status": "approved"}}}, {"name": 1, "email": 1, "role": 1}).to_list(length=None)
        contacts = caregivers
    else:
        raise HTTPException(403, "Chat is only available to caregivers and experts")
    for contact in contacts:
        contact_id = contact["_id"]
        contact["lastMessage"] = await database.messages.find_one({"$or": [{"senderId": user["_id"], "receiverId": contact_id}, {"senderId": contact_id, "receiverId": user["_id"]}]}, sort=[("createdAt", -1)])
        contact["unreadCount"] = await database.messages.count_documents({"senderId": contact_id, "receiverId": user["_id"], "isRead": False})
    contacts.sort(key=lambda item: (item.get("lastMessage") or {}).get("createdAt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return {"success": True, "data": json_safe(contacts)}


@router.post("/upload")
async def upload_chat_file(file: UploadFile | None = File(default=None)):
    if file is None:
        raise HTTPException(400, "No file uploaded.")
    if file.content_type not in ALLOWED_FILES:
        raise HTTPException(400, "Unsupported file type. Allowed: images, PDF, Word, and plain text.")
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413, "File exceeds the 20 MB upload limit")
    extension = os.path.splitext(file.filename or "")[1].lower()
    filename = f"{int(datetime.now().timestamp() * 1000)}-{secrets.token_hex(5)}{extension}"
    with open(os.path.join(CHAT_UPLOAD_DIR, filename), "wb") as uploaded:
        uploaded.write(data)
    return {"success": True, "data": {"fileUrl": f"/uploads/chat/{filename}", "fileName": file.filename or filename, "type": "image" if file.content_type.startswith("image/") else "file"}}


@router.post("/ai")
async def ai_chat(payload: dict):
    message = str(payload.get("message", "")).strip()
    if not message:
        raise HTTPException(400, "Message is required.")
    reply = await chat_reply(message, payload.get("history", []))
    return {"success": True, "data": {"reply": reply}}


@router.get("/{contact_id}/messages")
async def get_messages(contact_id: str, page: int = 1, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    contact = object_id(contact_id, "contact id")
    if not await _can_chat(database, user, contact):
        raise HTTPException(403, "You are not assigned to chat with this contact")
    page = max(page, 1)
    cursor = database.messages.find({"$or": [{"senderId": user["_id"], "receiverId": contact}, {"senderId": contact, "receiverId": user["_id"]}]}).sort("createdAt", -1).skip((page - 1) * 50).limit(50)
    messages = await cursor.to_list(length=50)
    messages.reverse()
    return {"success": True, "data": json_safe(messages)}


async def _save_message(database: AsyncIOMotorDatabase, sender_id: ObjectId, receiver_id: ObjectId, payload: dict):
    now = datetime.now(timezone.utc)
    message = {"senderId": sender_id, "receiverId": receiver_id, "content": payload.get("content") or "", "type": payload.get("type", "text"), "fileUrl": payload.get("fileUrl"), "fileName": payload.get("fileName"), "isRead": False, "createdAt": now, "updatedAt": now}
    if message["type"] not in {"text", "file", "image"}:
        raise HTTPException(400, "Invalid message type")
    result = await database.messages.insert_one(message)
    message["_id"] = result.inserted_id
    return message


@router.post("/{contact_id}/message", status_code=201)
async def send_message(contact_id: str, payload: dict, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    contact = object_id(contact_id, "contact id")
    if not await _can_chat(database, user, contact):
        raise HTTPException(403, "You are not assigned to chat with this contact")
    message = await _save_message(database, user["_id"], contact, payload)
    serialized = json_safe(message)
    await socket_server.emit("receive_message", serialized, room=str(contact))
    return {"success": True, "data": serialized}


@router.patch("/{contact_id}/read")
async def mark_read(contact_id: str, user: dict = Depends(get_current_user), database: AsyncIOMotorDatabase = Depends(get_database)):
    contact = object_id(contact_id, "contact id")
    if not await _can_chat(database, user, contact):
        raise HTTPException(403, "You are not assigned to chat with this contact")
    await database.messages.update_many({"senderId": contact, "receiverId": user["_id"], "isRead": False}, {"$set": {"isRead": True}})
    return {"success": True}


@socket_server.event
async def connect(sid, environ, auth):
    token = (auth or {}).get("token")
    if not token or not settings.jwt_secret:
        return False
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        user_id = ObjectId(claims["id"])
        database = _database_from_app()
        user = await database.users.find_one({"_id": user_id})
        if not user:
            return False
    except (JWTError, KeyError, ValueError, TypeError):
        return False
    await socket_server.save_session(sid, {"userId": str(user_id), "role": user.get("role")})
    await socket_server.enter_room(sid, str(user_id))
    return True


def _database_from_app() -> AsyncIOMotorDatabase:
    from .core import get_database

    return get_database()


@socket_server.on("send_message")
async def socket_send_message(sid, data):
    session = await socket_server.get_session(sid)
    try:
        sender_id = ObjectId(session["userId"])
        receiver_id = object_id(str(data.get("receiverId", "")), "receiver id")
        database = _database_from_app()
        sender = await database.users.find_one({"_id": sender_id})
        if not sender or not await _can_chat(database, sender, receiver_id):
            raise ValueError("You are not assigned to chat with this contact")
        message = await _save_message(database, sender_id, receiver_id, data)
        serialized = json_safe(message)
        await socket_server.emit("receive_message", serialized, room=str(receiver_id))
        await socket_server.emit("message_sent", serialized, room=sid)
    except Exception as error:
        await socket_server.emit("message_error", {"error": str(error)}, room=sid)


@socket_server.on("typing")
async def socket_typing(sid, data):
    session = await socket_server.get_session(sid)
    receiver_id = str((data or {}).get("receiverId", ""))
    if re.fullmatch(r"[0-9a-fA-F]{24}", receiver_id):
        await socket_server.emit("user_typing", {"senderId": session["userId"]}, room=receiver_id, skip_sid=sid)


@socket_server.on("stop_typing")
async def socket_stop_typing(sid, data):
    session = await socket_server.get_session(sid)
    receiver_id = str((data or {}).get("receiverId", ""))
    if re.fullmatch(r"[0-9a-fA-F]{24}", receiver_id):
        await socket_server.emit("user_stop_typing", {"senderId": session["userId"]}, room=receiver_id, skip_sid=sid)