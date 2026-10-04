"""Seed the four accounts test_access_control_e2e.py expects into a DISPOSABLE database.

    DATABASE_URL=postgresql+asyncpg://zenplus:...@127.0.0.1:55432/zenplus \
    PYTHONPATH=server python server/tests/integration/access_control/seed.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from sqlalchemy import text  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.core.security import hash_password  # noqa: E402

USERS = [
    ("admin", "admin", "AdminPassw0rd-Scratch!"),
    ("viewer1", "viewer", "ViewerPassw0rd-Scratch!"),
    ("operator1", "operator", "OperatorPassw0rd-Scratch!"),
    ("readonly1", "read_only", "ReadOnlyPassw0rd-Scratch!"),
]


async def main() -> None:
    async with AsyncSessionLocal() as db:
        for username, role, password in USERS:
            await db.execute(
                text("INSERT INTO users (username,email,password_hash,role,full_name) "
                     "VALUES (:u,:e,:p,:r,:f) ON CONFLICT (username) DO UPDATE "
                     "SET password_hash=EXCLUDED.password_hash, role=EXCLUDED.role, is_active=true"),
                {"u": username, "e": f"{username}@example.test", "p": hash_password(password),
                 "r": role, "f": username.title()},
            )
        await db.commit()


asyncio.run(main())
