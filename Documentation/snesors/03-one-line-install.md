# Ubuntu One-Line Sensor Install

## Goal

From the main appliance UI, an admin creates a sensor and copies one command.
That command is run on an Ubuntu sensor machine and turns it into a registered
ZenPlus remote sensor.

## Target Command

The UI should generate this shape:

```bash
curl -fsSL https://<main-appliance>/api/v1/sensor/install.sh | sudo env \
  ZENPLUS_SERVER_URL="https://<main-appliance>" \
  ZENPLUS_ENROLLMENT_TOKEN="<one-time-token>" \
  ZENPLUS_SENSOR_NAME="branch-a" \
  bash
```

For an HTTP lab appliance:

```bash
curl -fsSL http://10.12.50.84/api/v1/sensor/install.sh | sudo env \
  ZENPLUS_SERVER_URL="http://10.12.50.84" \
  ZENPLUS_ENROLLMENT_TOKEN="<one-time-token>" \
  ZENPLUS_SENSOR_NAME="lab-sensor-01" \
  ZENPLUS_VERIFY_TLS="0" \
  bash
```

Production should use HTTPS and `ZENPLUS_VERIFY_TLS=1`.

## What The Installer Does

The controller-served install script should:

1. verify it is running as root
2. detect architecture: `amd64` first, `arm64` later
3. install minimal prerequisites:
   - `curl`
   - `ca-certificates`
   - `iputils-ping`
   - optional `snmp` package later
4. create system user:
   - user: `zenplus-sensor`
   - no shell or `/usr/sbin/nologin`
5. create directories:
   - `/etc/zenplus-sensor`
   - `/var/lib/zenplus-sensor`
   - `/var/log/zenplus-sensor`
   - `/usr/local/bin`
6. download the prebuilt `zenplus-sensor` binary
7. verify checksum and later signature
8. install the binary as `/usr/local/bin/zenplus-sensor`
9. set `cap_net_raw+ep` for ICMP if available
10. write `/etc/zenplus-sensor/sensor.env`
11. write `/etc/systemd/system/zenplus-sensor.service`
12. run `systemctl daemon-reload`
13. enable and start the service
14. print status and next steps

## Installer Artifacts

The main appliance should serve:

```text
GET /api/v1/sensor/install.sh
GET /api/v1/sensor/bin/linux-amd64/zenplus-sensor
GET /api/v1/sensor/bin/linux-amd64/zenplus-sensor.sha256
GET /api/v1/sensor/bin/linux-amd64/manifest.json
```

Later:

```text
GET /api/v1/sensor/bin/linux-amd64/manifest.json.sig
GET /api/v1/sensor/bin/linux-arm64/zenplus-sensor
GET /api/v1/sensor/packages/zenplus-sensor.deb
```

## Environment File

`/etc/zenplus-sensor/sensor.env`:

```bash
ZENPLUS_SERVER_URL=https://main-appliance.example.com
ZENPLUS_ENROLLMENT_TOKEN=zps_enr_xxx
ZENPLUS_SENSOR_NAME=branch-a
ZENPLUS_VERIFY_TLS=1
ZENPLUS_SENSOR_STATE_DIR=/var/lib/zenplus-sensor
ZENPLUS_HEARTBEAT_INTERVAL_SECONDS=30
ZENPLUS_CONFIG_POLL_INTERVAL_SECONDS=60
ZENPLUS_UPLOAD_INTERVAL_SECONDS=10
ZENPLUS_MAX_WORKERS=100
```

After successful enrollment, the sensor should remove or blank
`ZENPLUS_ENROLLMENT_TOKEN` from disk so the one-time token is not retained.

## Systemd Unit

```ini
[Unit]
Description=ZenPlus Remote Sensor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=zenplus-sensor
Group=zenplus-sensor
EnvironmentFile=/etc/zenplus-sensor/sensor.env
ExecStart=/usr/local/bin/zenplus-sensor
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=/var/lib/zenplus-sensor /var/log/zenplus-sensor /etc/zenplus-sensor
AmbientCapabilities=CAP_NET_RAW
CapabilityBoundingSet=CAP_NET_RAW

[Install]
WantedBy=multi-user.target
```

## Sensor CLI

The binary should eventually support:

```bash
zenplus-sensor status
zenplus-sensor diagnose
zenplus-sensor enroll
zenplus-sensor reset
zenplus-sensor version
```

MVP can rely on:

```bash
systemctl status zenplus-sensor
journalctl -u zenplus-sensor -n 100 --no-pager
cat /var/lib/zenplus-sensor/state.json
```

## Controller UI Copy

The Add Sensor dialog should show:

- sensor name
- site
- token expiry
- install one-liner
- copy button
- warning that token is shown once
- command for HTTP/lab and HTTPS/production if needed

## Acceptance Criteria

On a fresh Ubuntu sensor machine:

1. one command installs the sensor
2. `systemctl is-active zenplus-sensor` returns `active`
3. `/var/lib/zenplus-sensor/state.json` contains `sensor_id` and API key
4. main appliance shows sensor `online`
5. heartbeat updates every 30 seconds
6. assigned service check results appear in analytics/storage
7. rebooting sensor keeps it enrolled and active

## Initial Test Script

After creating a sensor in the UI and copying the command:

```bash
sudo systemctl status zenplus-sensor --no-pager
sudo journalctl -u zenplus-sensor -n 100 --no-pager
sudo cat /var/lib/zenplus-sensor/state.json
```

On the main appliance:

```bash
curl -fsS http://127.0.0.1:8000/api/v1/system/health
```

Then check `Settings -> Sensors`.
