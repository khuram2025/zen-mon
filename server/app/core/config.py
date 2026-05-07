from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # App
    APP_NAME: str = "ZenPlus API"
    DEBUG: bool = False
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000

    # PostgreSQL
    DATABASE_URL: str = "postgresql+asyncpg://zenplus:changeme@localhost:5432/zenplus"

    # ClickHouse
    CLICKHOUSE_HOST: str = "localhost"
    CLICKHOUSE_PORT: int = 9000           # Native protocol (Go poller)
    CLICKHOUSE_HTTP_PORT: int = 8123      # HTTP protocol (Python clickhouse-connect)
    CLICKHOUSE_DB: str = "zenplus"
    CLICKHOUSE_USER: str = "default"
    CLICKHOUSE_PASSWORD: str = "changeme"

    # Redis
    REDIS_URL: str = "redis://:changeme@localhost:6379/0"

    # JWT
    JWT_SECRET: str = "changeme"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 1440  # 24 hours

    # SNMP credential encryption
    # 32-byte key, base64 or hex encoded. Rotated via scripts/rotate-snmp-key.sh.
    SNMP_ENC_KEY: str = ""

    # CORS — .env values override these defaults
    CORS_ORIGINS: list[str] = ["*"]

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
