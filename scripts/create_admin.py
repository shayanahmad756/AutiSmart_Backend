import asyncio
from getpass import getpass

from motor.motor_asyncio import AsyncIOMotorClient

from app.core import hash_password, settings


async def main():
    if not settings.mongo_uri:
        raise SystemExit("Set MONGO_URI in backend/.env first")
    email = input("Admin email [admin@autismart.com]: ").strip().lower() or "admin@autismart.com"
    password = getpass("Admin password (hidden): ")
    if len(password) < 6:
        raise SystemExit("Password must be at least 6 characters")
    client = AsyncIOMotorClient(settings.mongo_uri)
    try:
        database_name = settings.mongo_db
        from urllib.parse import unquote, urlsplit
        database_name = unquote(urlsplit(settings.mongo_uri).path.lstrip("/").split("/", 1)[0]) or database_name
        database = client[database_name]
        if await database.users.find_one({"email": email}):
            raise SystemExit(f"An account with {email} already exists")
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        await database.users.insert_one({"name": "Admin User", "email": email, "password": hash_password(password), "role": "admin", "isVerified": True, "expertRequests": [], "createdAt": now, "updatedAt": now})
        print(f"Admin account created: {email}")
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())