# Startup & Testing Guide

## Prerequisites

Install these on your system:
- **Docker** 24+ with Docker Compose v2
- **Go** 1.22+ (for local poller development)
- **Python** 3.12+ with pip (for local API development)
- **Node.js** 22+ with npm (for local dashboard development)

## Quick Start (Docker Compose)

```bash
# 1. Clone the repo and set up environment
cd /home/net
cp .env.example .env

# 2. Start all infrastructure services
docker compose up -d postgres clickhouse redis

# 3. Wait for services to be healthy
docker compose ps   # All should show "healthy"

# 4. Start the application services
docker compose up -d poller server dashboard

# 5. Access the dashboard
open http://localhost:3000

# Login: admin / admin123
```

## Local Development (Without Docker for App Services)

### Start infrastructure only
```bash
docker compose up -d postgres clickhouse redis
```

### Run Go Poller locally
```bash
cd poller
go mod tidy
go run ./cmd/poller
```

### Run FastAPI server locally
```bash
cd server
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### Run React dashboard locally
```bash
cd dashboard
npm install
npm run dev
# Runs at http://localhost:5173 with API proxy to :8000
```

## Testing Checklist

### 1. Infrastructure Health
```bash
# PostgreSQL
docker exec zenplus-postgres psql -U zenplus -c "SELECT count(*) FROM devices;"
# Should return: 27 (seeded devices)

# ClickHouse
docker exec zenplus-clickhouse clickhouse-client --password clickhouse_dev \
  -q "SELECT count(*) FROM zenplus.ping_metrics;"

# Redis
docker exec zenplus-redis redis-cli -a redis_dev ping
# Should return: PONG
```

### 2. API Health
```bash
# Health check
curl http://localhost:8000/api/v1/system/health

# Login
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "admin123"}'
# Save the access_token from response

# List devices (with token)
curl http://localhost:8000/api/v1/devices \
  -H "Authorization: Bearer <token>"

# Device summary
curl http://localhost:8000/api/v1/devices/summary \
  -H "Authorization: Bearer <token>"
```

### 3. Poller Health
```bash
curl http://localhost:8081/health
# Should return device count and poller status
```

### 4. Real-time Streams
```bash
# SSE metrics stream (will show live data)
curl -N http://localhost:8000/api/v1/stream/metrics

# SSE status changes
curl -N http://localhost:8000/api/v1/stream/status
```

### 5. Dashboard Verification
1. Open http://localhost:3000
2. Login with admin / admin123
3. Verify KPI cards show device counts
4. Verify device status heatmap renders
5. Navigate to Devices page - verify table loads
6. Click a device - verify detail page with chart
7. Navigate to Alerts page - verify tabs work

## Troubleshooting

### Poller can't ping devices
```bash
# Check if ICMP is allowed
sysctl net.ipv4.ping_group_range
# Should be: 0 2147483647

# Or give the binary CAP_NET_RAW
sudo setcap cap_net_raw+ep $(which zenplus-poller)
```

### ClickHouse connection refused
```bash
# Check if ClickHouse is ready
docker logs zenplus-clickhouse | tail -5
# Verify port mapping
docker port zenplus-clickhouse
```

### Frontend can't reach API
```bash
# Check CORS settings in server/app/core/config.py
# In dev mode, Vite proxies /api to localhost:8000
# In Docker, nginx proxies /api to server:8000
```
