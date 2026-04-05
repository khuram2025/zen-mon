# ZenPlus - Network Monitoring System

Production-grade network monitoring platform with ICMP ping monitoring, real-time dashboards, intelligent alerting, and SMS/Email notifications.

![Stack](https://img.shields.io/badge/Go-Poller-00ADD8?style=flat&logo=go)
![Stack](https://img.shields.io/badge/FastAPI-Backend-009688?style=flat&logo=fastapi)
![Stack](https://img.shields.io/badge/React-Dashboard-61DAFB?style=flat&logo=react)
![Stack](https://img.shields.io/badge/ClickHouse-Metrics-FFCC01?style=flat&logo=clickhouse)
![Stack](https://img.shields.io/badge/PostgreSQL-Config-336791?style=flat&logo=postgresql)

---

## Quick Install (One Line)

```bash
curl -fsSL https://raw.githubusercontent.com/khuram2025/zen-mon/main/install.sh | sudo bash
```

**Requirements:** Ubuntu 20.04+ (fresh or existing server), root/sudo access, 4GB+ RAM

The installer automatically handles everything:
- Docker, Go, Node.js, Python, Nginx
- PostgreSQL, ClickHouse, Redis databases
- Builds and configures all services
- Sets up systemd services + Nginx reverse proxy
- Creates `zenplus` management CLI

After install, open `http://<your-server-ip>` and login with `admin` / `admin123`.

---

## Management

```bash
zenplus status       # Show all services and access URLs
zenplus update       # One-command upgrade to latest version
zenplus restart      # Restart all services
zenplus stop         # Stop application services
zenplus start        # Start all services
zenplus logs api     # View API logs (also: poller, all)
zenplus backup       # Backup PostgreSQL database
```

### Update to Latest Version

```bash
sudo zenplus update
```

This pulls latest code from GitHub, rebuilds all components, runs database migrations, and restarts services.

---

## What Gets Installed

| Component | Technology | Purpose |
|-----------|-----------|---------|
| **Dashboard** | React + TypeScript + Tailwind | Dark-mode monitoring UI on port 80 |
| **API** | FastAPI (Python) | REST API + SSE streaming on port 8000 |
| **Poller** | Go | ICMP ping engine, 10K+ devices/second |
| **Metrics DB** | ClickHouse | Time-series storage with auto-rollup |
| **Config DB** | PostgreSQL | Devices, users, alerts, rules |
| **Cache** | Redis | Real-time pub/sub for live updates |
| **Proxy** | Nginx | Serves dashboard + proxies API |

## Architecture

```
Browser → Nginx (:80) → React Dashboard
                      → FastAPI API (:8000) → PostgreSQL (config)
                                             → ClickHouse (metrics)
                                             → Redis (real-time)
Go Poller → ICMP Ping → ClickHouse (write metrics)
          → PostgreSQL (read devices, write status)
          → Redis (publish events)
          → FastAPI (trigger alert evaluation)
```

---

## Features

### Device Monitoring
- ICMP ping monitoring with configurable intervals (15s - 10min)
- Batch scheduling for 10,000+ devices per poller
- Automatic status detection: UP / DOWN / DEGRADED
- Device types: Router, Switch, Firewall, Server, Access Point, Printer

### Dashboard
- Real-time dark-mode UI with auto-refresh
- KPI cards, status heatmap, device table with advanced filters
- Device detail page with RTT/packet loss charts, uptime timeline, incident history
- Filter by: Status, Device Type, Group, Location

### Device Management
- Add single devices with visual type selector
- Bulk import from CSV or JSON (with template downloads)
- Export device inventory
- Bulk delete with confirmation dialog

### Alerting System
- Intelligent alert rules: trigger on DOWN, UP, DEGRADED, or any status change
- Scope rules by: All devices, Device Type, Group, Location, or specific device
- Recovery alerts: notify when device comes back up
- Schedule: active hours + active days (e.g., Mon-Fri 8am-6pm only)
- Timing: cooldown, minimum duration before alert, max repeat count
- Customizable email/SMS message templates with variables:
  `{hostname}`, `{ip_address}`, `{status}`, `{severity}`, `{group}`, `{location}`, `{device_type}`, `{timestamp}`

### Notifications
- **Email**: SMTP gateway (Gmail, SendGrid, any SMTP server)
- **SMS**: Twilio, Vonage, or Custom HTTP API (Cequens, etc.)
- **Webhook**: POST to any URL
- **Slack**: Webhook integration
- **Telegram**: Bot API integration
- Multiple gateways per type
- Test button on every channel
- Alert rule preview (email + SMS mockup) and simulate (send real test)

### Data Storage
- Raw metrics: 30-day retention
- 5-minute rollups: 90-day retention (auto materialized view)
- 1-hour rollups: 1-year retention (auto materialized view)
- Incident history with timestamps and duration

---

## Ports

| Port | Service | Access |
|------|---------|--------|
| 80 | Nginx (Dashboard + API proxy) | Public - user access |
| 8000 | FastAPI API | Internal (proxied by Nginx) |
| 8081 | Go Poller health check | Internal |
| 5432 | PostgreSQL | Docker internal |
| 8123 | ClickHouse HTTP | Docker internal |
| 9000 | ClickHouse Native | Docker internal |
| 6379 | Redis | Docker internal |

## Default Credentials

| Service | Username | Password |
|---------|----------|----------|
| Dashboard | `admin` | `admin123` |
| PostgreSQL | `zenplus` | Auto-generated (in `/opt/zenplus/.env`) |
| ClickHouse | `default` | Auto-generated (in `/opt/zenplus/.env`) |

**Change the admin password after first login.**

---

## Manual Installation (Development)

```bash
# Clone
git clone https://github.com/khuram2025/zen-mon.git
cd zen-mon

# Start databases
cp .env.example .env
docker compose up -d postgres clickhouse redis

# Go Poller
cd poller && go mod tidy && go run ./cmd/poller

# FastAPI Server (new terminal)
cd server && python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt && pip install 'bcrypt==4.2.1'
uvicorn app.main:app --reload --port 8000

# React Dashboard (new terminal)
cd dashboard && npm install && npx vite --port 3000
```

Dashboard: `http://localhost:3000` | API: `http://localhost:8000/docs`

---

## Troubleshooting

### Services not starting
```bash
sudo zenplus status                    # Check all services
sudo journalctl -u zenplus-api -n 50   # API logs
sudo journalctl -u zenplus-poller -n 50 # Poller logs
```

### Port conflict (old app on 8000)
```bash
sudo fuser -k 8000/tcp                # Kill whatever is on port 8000
sudo zenplus restart                   # Restart ZenPlus
```

### Poller can't ping devices
```bash
sudo sysctl net.ipv4.ping_group_range  # Should be: 0 2147483647
sudo setcap cap_net_raw+ep /opt/zenplus/bin/zenplus-poller
```

### Reset admin password
```bash
cd /opt/zenplus && source venv/bin/activate
HASH=$(python3 -c "from passlib.context import CryptContext; print(CryptContext(schemes=['bcrypt']).hash('admin123'))")
docker compose exec -T postgres psql -U zenplus -c "UPDATE users SET password_hash = '$HASH' WHERE username = 'admin';"
```

### Complete reinstall
```bash
sudo systemctl stop zenplus-api zenplus-poller
cd /opt/zenplus && sudo docker compose down -v
sudo rm -rf /opt/zenplus /usr/local/bin/zenplus
sudo rm -f /etc/systemd/system/zenplus-*.service
curl -fsSL https://raw.githubusercontent.com/khuram2025/zen-mon/main/install.sh | sudo bash
```

---

## License

MIT

## Contributing

Pull requests welcome. For major changes, please open an issue first.
