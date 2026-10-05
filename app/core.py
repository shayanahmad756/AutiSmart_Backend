from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import aiosmtplib
import bcrypt
from email.message import EmailMessage
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from bson import ObjectId


class Settings(BaseSettings):
    port: int = 5000
    node_env: str = "development"
    mongo_uri: str = ""
    mongo_db: str = "test"
    jwt_secret: str = ""
    groq_api_key: str = ""
    email: str = ""
    email_pass: str = ""
    ml_url: str = Field(default="http://localhost:5001", validation_alias=AliasChoices("ML_URL", "FLASK_URL"))

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


settings = Settings()
mongo_client: AsyncIOMotorClient | None = None
security = HTTPBearer(auto_error=False)


def get_database() -> AsyncIOMotorDatabase:
    if mongo_client is None:
        raise HTTPException(status_code=503, detail="Database is not connected")
    database_name = unquote(urlsplit(settings.mongo_uri).path.lstrip("/").split("/", 1)[0])
    return mongo_client[database_name or settings.mongo_db]


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    database: AsyncIOMotorDatabase = Depends(get_database),
) -> dict[str, Any]:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authorized, no token provided")
    if not settings.jwt_secret:
        raise HTTPException(status_code=503, detail="JWT_SECRET is not configured")
    try:
        payload = jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=["HS256"])
        user_id = payload.get("id")
        if not user_id:
            raise ValueError("Missing user id")
        user = await database.users.find_one({"_id": ObjectId(user_id)})
    except (JWTError, ValueError, TypeError):
        user = None
    if user is None:
        raise HTTPException(status_code=401, detail="Not authorized, token failed")
    return user


def require_roles(*roles: str):
    async def role_dependency(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
        if user.get("role") not in roles:
            raise HTTPException(status_code=403, detail="Access denied")
        return user

    return role_dependency


def create_token(user_id: ObjectId | str) -> str:
    if not settings.jwt_secret:
        raise HTTPException(status_code=503, detail="JWT_SECRET is not configured")
    expires = datetime.now(timezone.utc) + timedelta(days=7)
    return jwt.encode({"id": str(user_id), "exp": expires}, settings.jwt_secret, algorithm="HS256")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("utf-8")


def verify_password(password: str, hashed_password: str | None) -> bool:
    if not hashed_password:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))
    except ValueError:
        return False


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(user["_id"]),
        "name": user.get("name"),
        "email": user.get("email"),
        "role": user.get("role"),
    }


def json_safe(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        result = {key: json_safe(item) for key, item in value.items() if key not in {"password", "otp"}}
        if "_id" in value:
            result.setdefault("id", str(value["_id"]))
        return result
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


async def send_otp_email(email: str, otp: str, name: str, password_reset: bool = False) -> None:
    if not settings.email or not settings.email_pass:
        raise HTTPException(status_code=503, detail="Email is not configured")
    message = EmailMessage()
    message["From"] = f"AutiSmart <{settings.email}>"
    message["To"] = email
    message["Subject"] = "Password Reset - AutiSmart" if password_reset else "Email Verification - AutiSmart"
    action = "reset your password" if password_reset else "verify your email address"
    message.set_content(
        f"Hello {name},\n\nUse this one-time code to {action}: {otp}\n\n"
        "The code expires in 10 minutes. If you did not request this, ignore this email."
    )
    await aiosmtplib.send(
        message,
        hostname="smtp.gmail.com",
        port=587,
        start_tls=True,
        username=settings.email,
        password=settings.email_pass.replace(" ", ""),
        timeout=20,
    )


def new_otp() -> str:
    return f"{secrets.randbelow(900000) + 100000}"


def object_id(value: str, label: str = "id") -> ObjectId:
    if not ObjectId.is_valid(value):
        raise HTTPException(status_code=400, detail=f"Invalid {label}")
    return ObjectId(value)