# Deployment & Infrastructure

## Docker Compose (Development)

```yaml
# docker-compose.yml
version: '3.8'

services:
  # ─── Databases ───
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: zenplus
      POSTGRES_USER: zenplus
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-zenplus_dev}
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./scripts/init-postgres.sql:/docker-entrypoint-initdb.d/01-init.sql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U zenplus"]
      interval: 5s
      timeout: 5s
      retries: 5

  clickhouse:
    image: clickhouse/clickhouse-server:24-alpine
    environment:
      CLICKHOUSE_DB: zenplus
      CLICKHOUSE_USER: default
      CLICKHOUSE_PASSWORD: ${CLICKHOUSE_PASSWORD:-clickhouse_dev}
    ports:
      - "8123:8123"   # HTTP
      - "9000:9000"   # Native
    volumes:
      - clickhouse_data:/var/lib/clickhouse
      - ./scripts/init-clickhouse.sql:/docker-entrypoint-initdb.d/01-init.sql
    healthcheck:
      test: ["CMD", "clickhouse-client", "--query", "SELECT 1"]
      interval: 5s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    command: redis-server --requirepass ${REDIS_PASSWORD:-redis_dev}
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "${REDIS_PASSWORD:-redis_dev}", "ping"]
      interval: 5s
      timeout: 5s
      retries: 5

  # ─── Application ───
  poller:
    build:
      context: ./poller
      dockerfile: Dockerfile
    cap_add:
      - NET_RAW
    depends_on:
      postgres:
        condition: service_healthy
      clickhouse:
        condition: service_healthy
      redis:
        condition: service_healthy
    environment:
      - POSTGRES_HOST=postgres
      - CLICKHOUSE_HOST=clickhouse
      - REDIS_HOST=redis
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD:-zenplus_dev}
      - CLICKHOUSE_PASSWORD=${CLICKHOUSE_PASSWORD:-clickhouse_dev}
      - REDIS_PASSWORD=${REDIS_PASSWORD:-redis_dev}
    volumes:
      - ./poller/config.yaml:/app/config.yaml
    network_mode: host  # Required for ICMP to reach external devices
    restart: unless-stopped

  server:
    build:
      context: ./server
      dockerfile: Dockerfile
    depends_on:
      postgres:
        condition: service_healthy
      clickhouse:
        condition: service_healthy
      redis:
        condition: service_healthy
    environment:
      - DATABASE_URL=postgresql+asyncpg://zenplus:${POSTGRES_PASSWORD:-zenplus_dev}@postgres:5432/zenplus
      - CLICKHOUSE_HOST=clickhouse
      - CLICKHOUSE_PASSWORD=${CLICKHOUSE_PASSWORD:-clickhouse_dev}
      - REDIS_URL=redis://:${REDIS_PASSWORD:-redis_dev}@redis:6379/0
      - JWT_SECRET=${JWT_SECRET:-change-me-in-production}
    ports:
      - "8000:8000"
    restart: unless-stopped

  dashboard:
    build:
      context: ./dashboard
      dockerfile: Dockerfile
    ports:
      - "3000:80"
    depends_on:
      - server
    restart: unless-stopped

volumes:
  postgres_data:
  clickhouse_data:
  redis_data:
```

## Directory Structure (Final)

```
zenplus/
├── docker-compose.yml
├── .env.example
├── .gitignore
├── README.md
│
├── Documentation/           # Design docs & task lists
│   ├── 01-PROJECT-OVERVIEW.md
│   ├── 02-TECH-STACK.md
│   ├── 03-DATABASE-SCHEMA.md
│   ├── 04-API-DESIGN.md
│   ├── 05-UI-DESIGN.md
│   ├── 06-TASK-LIST.md
│   ├── 07-GO-POLLER-DESIGN.md
│   └── 08-DEPLOYMENT.md
│
├── scripts/                 # Database init & utilities
│   ├── init-postgres.sql
│   └── init-clickhouse.sql
│
├── poller/                  # Go ping engine
│   ├── cmd/poller/main.go
│   ├── internal/
│   ├── config.yaml
│   ├── Dockerfile
│   ├── go.mod
│   └── go.sum
│
├── server/                  # FastAPI backend
│   ├── app/
│   ├── requirements.txt
│   ├── Dockerfile
│   └── alembic.ini
│
└── dashboard/               # React frontend
    ├── src/
    ├── package.json
    ├── vite.config.ts
    ├── tailwind.config.ts
    └── Dockerfile
```

## Environment Variables

```bash
# .env.example
# Database
POSTGRES_PASSWORD=change-me
CLICKHOUSE_PASSWORD=change-me
REDIS_PASSWORD=change-me

# API
JWT_SECRET=generate-a-random-64-char-string
API_HOST=0.0.0.0
API_PORT=8000

# Poller
POLLER_ID=poller-01

# Frontend
VITE_API_URL=http://localhost:8000
```

## Production Considerations

### Horizontal Scaling
```
                    ┌─────────────┐
                    │ Load Balancer│
                    │   (Caddy)   │
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │ FastAPI  │ │ FastAPI  │ │ FastAPI  │
        │ Worker 1 │ │ Worker 2 │ │ Worker 3 │
        └──────────┘ └──────────┘ └──────────┘
              │            │            │
              └────────────┼────────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │ Poller   │ │ Poller   │ │ Poller   │
        │ Region A │ │ Region B │ │ Region C │
        │ (5K dev) │ │ (3K dev) │ │ (2K dev) │
        └──────────┘ └──────────┘ └──────────┘
```

### Resource Requirements (Phase 1)

| Service | CPU | RAM | Disk |
|---------|-----|-----|------|
| PostgreSQL | 1 core | 1 GB | 10 GB |
| ClickHouse | 2 cores | 4 GB | 50 GB |
| Redis | 0.5 core | 512 MB | 1 GB |
| Go Poller | 1 core | 256 MB | minimal |
| FastAPI | 1 core | 512 MB | minimal |
| React (Nginx) | 0.5 core | 128 MB | minimal |
| **Total** | **6 cores** | **6.5 GB** | **61 GB** |

Minimum viable: 4 cores, 8 GB RAM, 100 GB SSD
