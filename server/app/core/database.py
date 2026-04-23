from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
import clickhouse_connect

from app.core.config import get_settings

settings = get_settings()

# PostgreSQL async engine
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


_clickhouse_client = None


def get_clickhouse_client():
    # Singleton: clickhouse_connect.HttpClient wraps a urllib3 PoolManager
    # which is thread-safe and reuses sockets. Creating a fresh client per
    # request leaks file descriptors until the process hits ulimit -n.
    global _clickhouse_client
    if _clickhouse_client is None:
        _clickhouse_client = clickhouse_connect.get_client(
            host=settings.CLICKHOUSE_HOST,
            port=settings.CLICKHOUSE_HTTP_PORT,
            database=settings.CLICKHOUSE_DB,
            username=settings.CLICKHOUSE_USER,
            password=settings.CLICKHOUSE_PASSWORD,
        )
    return _clickhouse_client
