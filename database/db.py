import motor.motor_asyncio
from config import DB_URI, DB_NAME


class Database:
    def __init__(self, uri: str, db_name: str):
        self._client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self._client[db_name]
        self.users = self.db.users

    async def is_user_exist(self, user_id: int) -> bool:
        return bool(await self.users.find_one({"id": user_id}))

    async def add_user(self, user_id: int, name: str):
        await self.users.insert_one({
            "id": user_id,
            "name": name,
            "session": None,
            "api_id": None,
            "api_hash": None,
            "destination": None,    # NEW: target chat for downloads
            "dest_label": None,     # human-readable label
        })

    async def delete_user(self, user_id: int):
        await self.users.delete_many({"id": user_id})

    async def total_users_count(self) -> int:
        return await self.users.count_documents({})

    async def get_all_users(self):
        return self.users.find({})

    # ── Session ────────────────────────────────────────────────────────────────
    async def set_session(self, user_id: int, session):
        await self.users.update_one({"id": user_id}, {"$set": {"session": session}})

    async def get_session(self, user_id: int):
        doc = await self.users.find_one({"id": user_id})
        return doc.get("session") if doc else None

    # ── API ────────────────────────────────────────────────────────────────────
    async def set_api_id(self, user_id: int, api_id: int):
        await self.users.update_one({"id": user_id}, {"$set": {"api_id": api_id}})

    async def get_api_id(self, user_id: int):
        doc = await self.users.find_one({"id": user_id})
        return doc.get("api_id") if doc else None

    async def set_api_hash(self, user_id: int, api_hash: str):
        await self.users.update_one({"id": user_id}, {"$set": {"api_hash": api_hash}})

    async def get_api_hash(self, user_id: int):
        doc = await self.users.find_one({"id": user_id})
        return doc.get("api_hash") if doc else None

    # ── Destination ────────────────────────────────────────────────────────────
    async def set_destination(self, user_id: int, dest, label: str = None):
        await self.users.update_one(
            {"id": user_id},
            {"$set": {"destination": dest, "dest_label": label}}
        )

    async def get_destination(self, user_id: int):
        doc = await self.users.find_one({"id": user_id})
        if not doc:
            return None, None
        return doc.get("destination"), doc.get("dest_label")


db = Database(DB_URI, DB_NAME)
