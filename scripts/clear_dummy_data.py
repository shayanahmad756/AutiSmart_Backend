import argparse
import asyncio
from urllib.parse import unquote, urlsplit

from motor.motor_asyncio import AsyncIOMotorClient

from app.core import settings


async def main():
    parser = argparse.ArgumentParser(description="Delete non-admin AutiSmart data.")
    parser.add_argument("--confirm", action="store_true", help="Confirm permanent deletion")
    args = parser.parse_args()
    if not args.confirm:
        raise SystemExit("No data was deleted. Re-run with --confirm to permanently clear non-admin data.")
    if not settings.mongo_uri:
        raise SystemExit("Set MONGO_URI in backend/.env first")
    database_name = unquote(urlsplit(settings.mongo_uri).path.lstrip("/").split("/", 1)[0]) or settings.mongo_db
    client = AsyncIOMotorClient(settings.mongo_uri)
    try:
        database = client[database_name]
        for collection in ("children", "activities", "childquizzes", "assessmentresults", "messages"):
            result = await database[collection].delete_many({})
            print(f"Deleted {result.deleted_count} {collection}")
        result = await database.users.delete_many({"role": {"$ne": "admin"}})
        print(f"Deleted {result.deleted_count} non-admin users; admin users were preserved")
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())