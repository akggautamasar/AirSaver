import motor.motor_asyncio
from pymongo import ASCENDING
from time import time as ts
from config import DB_URI, DB_NAME


class Database:
    def __init__(self, uri: str, db_name: str):
        self._client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self._client[db_name]
        self.users = self.db.users
        self.batches = self.db.batches

    async def ensure_indexes(self):
        """Create indexes once on startup — no-op if they already exist."""
        await self.users.create_index([("id", ASCENDING)], unique=True, background=True)
        await self.batches.create_index(
            [("user_id", ASCENDING), ("batch_id", ASCENDING)], unique=True, background=True
        )
        await self.batches.create_index(
            [("user_id", ASCENDING), ("status", ASCENDING)], background=True
        )

    # ── User CRUD ──────────────────────────────────────────────────────────────
    async def is_user_exist(self, user_id: int) -> bool:
        return bool(await self.users.find_one({"id": user_id}, {"_id": 1}))

    async def add_user(self, user_id: int, name: str):
        await self.users.insert_one({
            "id": user_id,
            "name": name,
            "session": None,
            "api_id": None,
            "api_hash": None,
            "destination": None,
            "dest_label": None,
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
        doc = await self.users.find_one({"id": user_id}, {"session": 1})
        return doc.get("session") if doc else None

    async def get_user_session_data(self, user_id: int):
        """Return (session, api_id, api_hash) in one round-trip."""
        doc = await self.users.find_one(
            {"id": user_id}, {"session": 1, "api_id": 1, "api_hash": 1}
        )
        if not doc:
            return None, None, None
        return doc.get("session"), doc.get("api_id"), doc.get("api_hash")

    async def set_api_id(self, user_id: int, api_id: int):
        await self.users.update_one({"id": user_id}, {"$set": {"api_id": api_id}})

    async def get_api_id(self, user_id: int):
        doc = await self.users.find_one({"id": user_id}, {"api_id": 1})
        return doc.get("api_id") if doc else None

    async def set_api_hash(self, user_id: int, api_hash: str):
        await self.users.update_one({"id": user_id}, {"$set": {"api_hash": api_hash}})

    async def get_api_hash(self, user_id: int):
        doc = await self.users.find_one({"id": user_id}, {"api_hash": 1})
        return doc.get("api_hash") if doc else None

    # ── Destination ────────────────────────────────────────────────────────────
    async def set_destination(self, user_id: int, dest, label: str = None):
        await self.users.update_one(
            {"id": user_id},
            {"$set": {"destination": dest, "dest_label": label}}
        )

    async def get_destination(self, user_id: int):
        doc = await self.users.find_one({"id": user_id}, {"destination": 1, "dest_label": 1})
        if not doc:
            return None, None
        return doc.get("destination"), doc.get("dest_label")

    # ── Resumable batches ──────────────────────────────────────────────────────
    async def save_batch(self, user_id: int, batch_id: str, state: dict):
        """
        Save or update batch state.

        state schema:
          user_id, batch_id (auto), jobs (list of [chat_id, start, end, topic]),
          current_job_idx, current_msg_id, target_chat, target_label,
          total, done, success, skipped, failed,
          created_at, updated_at, status ('running'|'paused'|'done'|'cancelled')
        """
        state["user_id"] = user_id
        state["batch_id"] = batch_id
        state["updated_at"] = ts()
        await self.batches.update_one(
            {"user_id": user_id, "batch_id": batch_id},
            {"$set": state, "$setOnInsert": {"created_at": ts()}},
            upsert=True,
        )

    async def get_active_batches(self, user_id: int):
        """Get any incomplete batches for a user."""
        cursor = self.batches.find({
            "user_id": user_id,
            "status": {"$in": ["running", "paused"]},
        }).sort("updated_at", -1)
        return await cursor.to_list(length=10)

    async def get_batch(self, user_id: int, batch_id: str):
        return await self.batches.find_one({"user_id": user_id, "batch_id": batch_id})

    async def mark_batch_status(self, user_id: int, batch_id: str, status: str):
        await self.batches.update_one(
            {"user_id": user_id, "batch_id": batch_id},
            {"$set": {"status": status, "updated_at": ts()}},
        )

    async def update_batch_progress(self, user_id: int, batch_id: str,
                                     current_job_idx: int, current_msg_id: int,
                                     done: int, success: int, skipped: int, failed: int):
        """Lightweight progress update — called frequently during batch."""
        await self.batches.update_one(
            {"user_id": user_id, "batch_id": batch_id},
            {"$set": {
                "current_job_idx": current_job_idx,
                "current_msg_id": current_msg_id,
                "done": done,
                "success": success,
                "skipped": skipped,
                "failed": failed,
                "updated_at": ts(),
            }},
        )

    async def delete_batch(self, user_id: int, batch_id: str):
        await self.batches.delete_one({"user_id": user_id, "batch_id": batch_id})

    async def cleanup_old_batches(self, max_age_seconds: int = 7 * 86400):
        """Periodically remove batches older than 7 days regardless of status."""
        cutoff = ts() - max_age_seconds
        await self.batches.delete_many({"created_at": {"$lt": cutoff}})


db = Database(DB_URI, DB_NAME)
