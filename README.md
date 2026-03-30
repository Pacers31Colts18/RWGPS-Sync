# RWGPS-Sync
Sync data from Ride with GPS to a SQLLiteDB (and optionally Home Assistant with MQTT). Coded with help from Claude.AI

# Ride With GPS → Home Assistant Sync

A Docker container that pulls your [Ride With GPS](https://ridewithgps.com) ride history into a local SQLite database and publishes live sensors to Home Assistant via MQTT auto-discovery.

---

## What it does

- **Syncs all your rides** from Ride With GPS into a local SQLite database
- **Exposes 15 sensors** in Home Assistant automatically via MQTT discovery
- **Runs on a schedule** — polls for new rides every 30 minutes (configurable)
- **Incremental syncs** — after the first full historical sync, only new rides are fetched

### Sensors created in Home Assistant

All sensors appear under a single **Ride With GPS** device:

| Sensor | Description |
|---|---|
| Last Ride Name | Name of your most recent ride |
| Last Ride Date | Date/time of most recent ride |
| Last Ride Distance | Distance in miles |
| Last Ride Duration | Duration (h:mm:ss) |
| Last Ride Avg Speed | Average speed (mph) |
| Last Ride Avg HR | Average heart rate (bpm) |
| Last Ride Avg Power | Average power (W) |
| Total Rides | Lifetime ride count |
| Total Miles | Lifetime distance (mi) |
| Avg Speed | All-time average speed (mph) |
| Max Speed | All-time top speed (mph) |
| Avg Heart Rate | All-time average HR (bpm) |
| Max Heart Rate | All-time max HR (bpm) |
| Avg Power | All-time average power (W) |
| Peak Power | All-time peak power (W) |

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose (or [Portainer](https://www.portainer.io/))
- A [Ride With GPS](https://ridewithgps.com) account
- Home Assistant with the **Mosquitto broker** add-on installed *(optional — skip if you only want the local DB)*

---

## Step 1 — Get your Ride With GPS credentials

You need three values: an **API key**, an **auth token**, and your **user ID**. No OAuth setup is required.

1. Log in to [ridewithgps.com](https://ridewithgps.com)
2. Go to **Settings → Developer → API Clients**
3. Click **Create new API Client** — give it any name (e.g. "Home Assistant Sync")
4. Copy the **API Key** shown on the client page
5. Click **"Create new Auth Token"** — copy that token too
6. Your **User ID** is the number in your profile URL:
   `https://ridewithgps.com/users/`**`123456`**

> **Security note:** Treat your auth token like a password. It grants read access to your account. Store it only in your `docker-compose.yml` or environment file — never commit it to a public repo.

---

## Step 2 — Set up Home Assistant MQTT *(skip if DB-only)*

If you want the Home Assistant sensors, you need an MQTT broker running in HA:

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**
2. Search for **Mosquitto broker** and install it
3. Go to **Settings → People → Users** and create a dedicated MQTT user (e.g. `mqtt`)
4. Note the **IP address** of your Home Assistant host (e.g. `192.168.1.50`)
5. Start the Mosquitto add-on

---

## Step 3 — Place `sync.py` on your server

The container runs `sync.py` from a mounted volume. You need to put it in place before starting the container.

**Option A — via SSH:**
```bash
mkdir -p /your/path/rwgps-sync/app
mkdir -p /your/path/rwgps-sync/data
# then copy sync.py into /your/path/rwgps-sync/app/
```

**Option B — via Synology File Station / file manager:**
1. Create the folder `rwgps-sync/app/` on your NAS or server
2. Upload `sync.py` into it

---

## Step 4 — Configure `docker-compose.yml`

Create a `docker-compose.yml` file with the following contents, filling in your values:

```yaml
version: "3.9"

services:
  rwgps-sync:
    image: python:3.12-slim
    container_name: rwgps-sync
    restart: unless-stopped
    working_dir: /app
    command: >
      sh -c "pip install requests paho-mqtt==1.6.1 -q &&
             python -u sync.py"
    volumes:
      - /your/path/rwgps-sync/app:/app
      - /your/path/rwgps-sync/data:/data
    environment:
      # Ride With GPS credentials (from Step 1)
      RWGPS_API_KEY:    "your_api_key_here"
      RWGPS_AUTH_TOKEN: "your_auth_token_here"
      RWGPS_USER_ID:    "your_numeric_user_id"

      # Home Assistant MQTT (from Step 2) — set to "false" to disable
      MQTT_ENABLED:     "true"
      MQTT_HOST:        "192.168.1.x"
      MQTT_PORT:        "1883"
      MQTT_USER:        "mqtt"
      MQTT_PASS:        "your_mqtt_password"

      # Sync schedule
      SYNC_INTERVAL_MINUTES: "30"

      # Database path (inside the container)
      DB_PATH: "/data/rides.db"
```

Replace `/your/path/rwgps-sync` with the actual path on your host (e.g. `/volume2/docker/rwgps-sync` on Synology).

---

## Step 5 — Start the container

**Using Docker Compose:**
```bash
docker compose up -d
```

**Using Portainer:**
1. Go to **Stacks → Add Stack**
2. Paste the contents of your `docker-compose.yml`
3. Click **Deploy the stack**

---

## Step 6 — Verify it's working

Check the container logs. You should see:

```
=== Ride With GPS Sync starting ===
Connecting to MQTT broker 192.168.1.x:1883…
Published MQTT discovery configs for 15 sensors.
Known ride IDs in DB: 0
Fetching page 1 of 5…
Page 1: 100 new trips (total fetched: 100/450)
Fetching page 2 of 5…
...
Reached end of results (450 total).
New/updated trips fetched: 450
Total rides in DB: 450
Published state to MQTT.
Sleeping 30 minutes until next sync…
```

The first run performs a **full historical sync** of all your rides. Subsequent syncs are incremental.

---

## Step 7 — Add the dashboard to Home Assistant

1. In Home Assistant, go to **Settings → Dashboards → Add Dashboard**
2. Give it a name (e.g. "Cycling") and click Create
3. Click the **⋮ menu → Edit → Raw configuration editor**
4. Paste the contents of `rwgps_dashboard.yaml` and click Save

Your sensors will appear immediately. The history graphs fill in over time as new rides sync.

> **Tip:** The sensors appear under **Settings → Devices & Services → MQTT → Ride With GPS**. If you don't see them, check that the Mosquitto broker is running and that your MQTT credentials are correct.

---

## Querying your ride data directly

The SQLite database lives at the path you set for `DB_PATH` (default: `/data/rides.db` inside the container, mounted to `./data/rides.db` on your host).

Open it with [DB Browser for SQLite](https://sqlitebrowser.org/) or query it via SSH:

```bash
sqlite3 /your/path/rwgps-sync/data/rides.db
```

### Example queries

**Your 10 most recent rides:**
```sql
SELECT name, departed_at, distance_mi, avg_speed_mph
FROM rides
ORDER BY departed_at DESC
LIMIT 10;
```

**Monthly mileage totals:**
```sql
SELECT
  strftime('%Y-%m', departed_at) AS month,
  ROUND(SUM(distance_mi), 1)     AS miles,
  COUNT(*)                        AS rides
FROM rides
GROUP BY month
ORDER BY month DESC;
```

**Your highest power rides:**
```sql
SELECT name, departed_at, avg_watts, max_watts
FROM rides
WHERE avg_watts IS NOT NULL
ORDER BY avg_watts DESC
LIMIT 10;
```

**Rides over 50 miles:**
```sql
SELECT name, departed_at, distance_mi, elevation_gain_ft
FROM rides
WHERE distance_mi >= 50
ORDER BY distance_mi DESC;
```

---

## Configuration reference

| Environment variable | Default | Description |
|---|---|---|
| `RWGPS_API_KEY` | *(required)* | Your Ride With GPS API key |
| `RWGPS_AUTH_TOKEN` | *(required)* | Your Ride With GPS auth token |
| `RWGPS_USER_ID` | *(required)* | Your numeric Ride With GPS user ID |
| `MQTT_ENABLED` | `true` | Set to `false` to disable HA integration |
| `MQTT_HOST` | `homeassistant.local` | IP or hostname of your MQTT broker |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_USER` | *(empty)* | MQTT username |
| `MQTT_PASS` | *(empty)* | MQTT password |
| `SYNC_INTERVAL_MINUTES` | `30` | How often to poll for new rides |
| `DB_PATH` | `/data/rides.db` | Path to the SQLite database inside the container |

---

## Updating

```bash
docker compose pull
docker compose up -d
```

Your database in `./data/` persists across updates and rebuilds.

---

## Troubleshooting

**Sensors not appearing in Home Assistant**
- Confirm the Mosquitto broker add-on is running in HA
- Check that `MQTT_HOST`, `MQTT_USER`, and `MQTT_PASS` are correct
- Go to **Settings → Devices & Services → MQTT** and check for the Ride With GPS device

**Container exits immediately**
- Check logs for a missing environment variable — `RWGPS_API_KEY`, `RWGPS_AUTH_TOKEN`, and `RWGPS_USER_ID` are all required

**Sync fetches pages but saves nothing**
- Make sure `sync.py` is the latest version — earlier versions had a pagination bug that caused infinite looping without saving

**Auth token expired**
- Go to **Settings → Developer → API Clients** on ridewithgps.com, select your client, and create a new auth token. Update `RWGPS_AUTH_TOKEN` in your compose file and restart the container.

---

## License

MIT
