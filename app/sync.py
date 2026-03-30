#!/usr/bin/env python3
"""
Ride With GPS → SQLite + Home Assistant MQTT Sync
Polls the RWGPS API, stores rides in a local SQLite DB,
and publishes sensor states to HA via MQTT discovery.
"""

import os
import time
import json
import logging
import sqlite3
import requests
import paho.mqtt.client as mqtt
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Config from environment ────────────────────────────────────────────────────
RWGPS_API_KEY    = os.environ["RWGPS_API_KEY"]
RWGPS_AUTH_TOKEN = os.environ["RWGPS_AUTH_TOKEN"]
RWGPS_USER_ID    = os.environ["RWGPS_USER_ID"]

MQTT_HOST        = os.environ.get("MQTT_HOST", "homeassistant.local")
MQTT_PORT        = int(os.environ.get("MQTT_PORT", 1883))
MQTT_USER        = os.environ.get("MQTT_USER", "")
MQTT_PASS        = os.environ.get("MQTT_PASS", "")
MQTT_ENABLED     = os.environ.get("MQTT_ENABLED", "true").lower() == "true"

SYNC_INTERVAL    = int(os.environ.get("SYNC_INTERVAL_MINUTES", 30)) * 60
DB_PATH          = os.environ.get("DB_PATH", "/data/rides.db")

RWGPS_BASE       = "https://ridewithgps.com"
PAGE_SIZE        = 100

# ── Unit helpers ───────────────────────────────────────────────────────────────
def meters_to_miles(m):
    return round(m / 1609.344, 2) if m else 0.0

def mps_to_mph(mps):
    return round(mps * 2.23694, 2) if mps else 0.0

def seconds_to_hms(s):
    if not s:
        return "0:00:00"
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}"

# ── Database ───────────────────────────────────────────────────────────────────
def init_db(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rides (
            id                INTEGER PRIMARY KEY,
            name              TEXT,
            departed_at       TEXT,
            distance_mi       REAL,
            duration_s        INTEGER,
            moving_time_s     INTEGER,
            avg_speed_mph     REAL,
            max_speed_mph     REAL,
            elevation_gain_ft REAL,
            avg_hr            REAL,
            max_hr            REAL,
            avg_watts         REAL,
            max_watts         REAL,
            calories          REAL,
            locality          TEXT,
            fetched_at        TEXT
        )
    """)
    conn.commit()


def upsert_ride(conn, ride):
    conn.execute("""
        INSERT INTO rides (
            id, name, departed_at, distance_mi, duration_s, moving_time_s,
            avg_speed_mph, max_speed_mph, elevation_gain_ft,
            avg_hr, max_hr, avg_watts, max_watts, calories, locality, fetched_at
        ) VALUES (
            :id, :name, :departed_at, :distance_mi, :duration_s, :moving_time_s,
            :avg_speed_mph, :max_speed_mph, :elevation_gain_ft,
            :avg_hr, :max_hr, :avg_watts, :max_watts, :calories, :locality, :fetched_at
        )
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,
            departed_at=excluded.departed_at,
            distance_mi=excluded.distance_mi,
            duration_s=excluded.duration_s,
            moving_time_s=excluded.moving_time_s,
            avg_speed_mph=excluded.avg_speed_mph,
            max_speed_mph=excluded.max_speed_mph,
            elevation_gain_ft=excluded.elevation_gain_ft,
            avg_hr=excluded.avg_hr,
            max_hr=excluded.max_hr,
            avg_watts=excluded.avg_watts,
            max_watts=excluded.max_watts,
            calories=excluded.calories,
            locality=excluded.locality,
            fetched_at=excluded.fetched_at
    """, ride)
    conn.commit()


def get_known_ids(conn):
    rows = conn.execute("SELECT id FROM rides").fetchall()
    return {r[0] for r in rows}


def get_lifetime_stats(conn):
    row = conn.execute("""
        SELECT
            COUNT(*)                        AS total_rides,
            ROUND(SUM(distance_mi), 2)      AS total_miles,
            SUM(duration_s)                 AS total_duration_s,
            ROUND(AVG(avg_speed_mph), 2)    AS all_time_avg_speed,
            MAX(max_speed_mph)              AS all_time_max_speed,
            ROUND(AVG(avg_hr), 1)           AS all_time_avg_hr,
            MAX(max_hr)                     AS all_time_max_hr,
            ROUND(AVG(avg_watts), 1)        AS all_time_avg_watts,
            MAX(max_watts)                  AS all_time_max_watts
        FROM rides
    """).fetchone()
    keys = ["total_rides","total_miles","total_duration_s",
            "all_time_avg_speed","all_time_max_speed",
            "all_time_avg_hr","all_time_max_hr",
            "all_time_avg_watts","all_time_max_watts"]
    return dict(zip(keys, row)) if row else {}


# ── RWGPS API ──────────────────────────────────────────────────────────────────
def rwgps_headers():
    return {
        "x-rwgps-api-key":    RWGPS_API_KEY,
        "x-rwgps-auth-token": RWGPS_AUTH_TOKEN,
        "Accept":             "application/json",
    }


def fetch_trips_page(page):
    """Returns (trips_list, total_count)."""
    url = f"{RWGPS_BASE}/users/{RWGPS_USER_ID}/trips.json"
    resp = requests.get(url, headers=rwgps_headers(), params={
        "page": page, "page_size": PAGE_SIZE
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if isinstance(data, list):
        return data, len(data)

    if isinstance(data, dict):
        total = data.get("results_count", data.get("total", 0))
        for key in ("results", "trips", "records", "data"):
            if key in data and isinstance(data[key], list):
                return data[key], total

    log.warning("Could not parse trips from response: %s", str(data)[:200])
    return [], 0


def parse_trip(raw):
    eg_m = raw.get("elevation_gain", 0) or 0
    return {
        "id":                raw["id"],
        "name":              raw.get("name", "Unnamed Ride"),
        "departed_at":       raw.get("departed_at") or raw.get("created_at"),
        "distance_mi":       meters_to_miles(raw.get("distance", 0)),
        "duration_s":        raw.get("duration", 0),
        "moving_time_s":     raw.get("moving_time", 0),
        "avg_speed_mph":     mps_to_mph(raw.get("avg_speed", 0)),
        "max_speed_mph":     mps_to_mph(raw.get("max_speed", 0)),
        "elevation_gain_ft": round(eg_m * 3.28084, 1),
        "avg_hr":            raw.get("avg_hr") or raw.get("avg_heart_rate"),
        "max_hr":            raw.get("max_hr") or raw.get("max_heart_rate"),
        "avg_watts":         raw.get("avg_watts"),
        "max_watts":         raw.get("max_watts"),
        "calories":          raw.get("calories"),
        "locality":          raw.get("locality", ""),
        "fetched_at":        datetime.now(timezone.utc).isoformat(),
    }


def fetch_all_trips(known_ids, full_sync=False):
    trips = []
    page = 1
    total_count = None

    while True:
        log.info("Fetching page %d%s…", page,
                 f" of {-(-total_count // PAGE_SIZE)}" if total_count else "")
        raw_list, total_count = fetch_trips_page(page)

        if not raw_list:
            log.info("Empty page — done fetching.")
            break

        new_this_page = 0
        for raw in raw_list:
            parsed = parse_trip(raw)
            if parsed["id"] not in known_ids or full_sync:
                trips.append(parsed)
                new_this_page += 1

        fetched_so_far = (page - 1) * PAGE_SIZE + len(raw_list)
        log.info("Page %d: %d new trips (total fetched: %d/%s)",
                 page, new_this_page, fetched_so_far, total_count or "?")

        # Stop if incremental and all on this page are known
        if new_this_page == 0 and not full_sync:
            log.info("All trips on this page already known — stopping.")
            break

        # Stop if we've fetched everything
        if total_count and fetched_so_far >= total_count:
            log.info("Reached end of results (%d total).", total_count)
            break

        # Fallback: stop if short page
        if len(raw_list) < PAGE_SIZE:
            break

        page += 1
        time.sleep(0.5)

    return trips


# ── MQTT / Home Assistant ──────────────────────────────────────────────────────
DISCOVERY_PREFIX = "homeassistant"
NODE_ID          = "rwgps"

SENSORS = [
    ("total_rides",         "RWGPS Total Rides",         "total_rides",          None,  None,    "mdi:bike"),
    ("total_miles",         "RWGPS Total Miles",         "total_miles",          "mi",  None,    "mdi:map-marker-distance"),
    ("all_time_avg_speed",  "RWGPS Avg Speed",           "all_time_avg_speed",   "mph", None,    "mdi:speedometer"),
    ("all_time_max_speed",  "RWGPS Max Speed",           "all_time_max_speed",   "mph", None,    "mdi:speedometer-slow"),
    ("all_time_avg_hr",     "RWGPS Avg Heart Rate",      "all_time_avg_hr",      "bpm", None,    "mdi:heart-pulse"),
    ("all_time_max_hr",     "RWGPS Max Heart Rate",      "all_time_max_hr",      "bpm", None,    "mdi:heart"),
    ("all_time_avg_watts",  "RWGPS Avg Power",           "all_time_avg_watts",   "W",   "power", "mdi:lightning-bolt"),
    ("all_time_max_watts",  "RWGPS Max Power",           "all_time_max_watts",   "W",   "power", "mdi:lightning-bolt-circle"),
    ("last_ride_distance",  "RWGPS Last Ride Distance",  "last_ride_distance",   "mi",  None,    "mdi:bike-fast"),
    ("last_ride_duration",  "RWGPS Last Ride Duration",  "last_ride_duration",   None,  None,    "mdi:timer"),
    ("last_ride_name",      "RWGPS Last Ride Name",      "last_ride_name",       None,  None,    "mdi:tag"),
    ("last_ride_date",      "RWGPS Last Ride Date",      "last_ride_date",       None,  "timestamp", "mdi:calendar"),
    ("last_ride_avg_speed", "RWGPS Last Ride Avg Speed", "last_ride_avg_speed",  "mph", None,    "mdi:speedometer"),
    ("last_ride_avg_hr",    "RWGPS Last Ride Avg HR",    "last_ride_avg_hr",     "bpm", None,    "mdi:heart-pulse"),
    ("last_ride_avg_watts", "RWGPS Last Ride Avg Power", "last_ride_avg_watts",  "W",   "power", "mdi:lightning-bolt"),
]

STATE_TOPIC = f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/state"


def mqtt_connect():
    client = mqtt.Client(client_id="rwgps_sync", clean_session=True)
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    return client


def publish_discovery(client):
    device = {
        "identifiers": ["rwgps_sync"],
        "name": "Ride With GPS",
        "model": "RWGPS Sync",
        "manufacturer": "ridewithgps.com",
    }
    for uid, name, key, unit, dev_class, icon in SENSORS:
        config = {
            "name":           name,
            "unique_id":      f"rwgps_{uid}",
            "state_topic":    STATE_TOPIC,
            "value_template": f"{{{{ value_json.{key} }}}}",
            "icon":           icon,
            "device":         device,
        }
        if unit:
            config["unit_of_measurement"] = unit
        if dev_class:
            config["device_class"] = dev_class
        topic = f"{DISCOVERY_PREFIX}/sensor/{NODE_ID}/{uid}/config"
        client.publish(topic, json.dumps(config), retain=True)
    log.info("Published MQTT discovery configs for %d sensors.", len(SENSORS))


def build_state_payload(conn):
    stats = get_lifetime_stats(conn)
    last = conn.execute("""
        SELECT name, departed_at, distance_mi, duration_s,
               avg_speed_mph, avg_hr, avg_watts
        FROM rides ORDER BY departed_at DESC LIMIT 1
    """).fetchone()
    return {
        "total_rides":          stats.get("total_rides", 0),
        "total_miles":          stats.get("total_miles", 0.0),
        "all_time_avg_speed":   stats.get("all_time_avg_speed", 0.0),
        "all_time_max_speed":   stats.get("all_time_max_speed", 0.0),
        "all_time_avg_hr":      stats.get("all_time_avg_hr"),
        "all_time_max_hr":      stats.get("all_time_max_hr"),
        "all_time_avg_watts":   stats.get("all_time_avg_watts"),
        "all_time_max_watts":   stats.get("all_time_max_watts"),
        "last_ride_name":       last[0] if last else None,
        "last_ride_date":       last[1] if last else None,
        "last_ride_distance":   last[2] if last else None,
        "last_ride_duration":   seconds_to_hms(last[3]) if last else None,
        "last_ride_avg_speed":  last[4] if last else None,
        "last_ride_avg_hr":     last[5] if last else None,
        "last_ride_avg_watts":  last[6] if last else None,
    }


# ── Main loop ──────────────────────────────────────────────────────────────────
def run_sync(conn, mqtt_client=None, full_sync=False):
    known_ids = get_known_ids(conn)
    log.info("Known ride IDs in DB: %d", len(known_ids))

    new_trips = fetch_all_trips(known_ids, full_sync=full_sync)
    log.info("New/updated trips fetched: %d", len(new_trips))

    for trip in new_trips:
        upsert_ride(conn, trip)

    if new_trips and mqtt_client and MQTT_ENABLED:
        payload = build_state_payload(conn)
        mqtt_client.publish(STATE_TOPIC, json.dumps(payload), retain=True)
        log.info("Published state to MQTT.")

    total = conn.execute("SELECT COUNT(*) FROM rides").fetchone()[0]
    log.info("Total rides in DB: %d", total)


def main():
    log.info("=== Ride With GPS Sync starting ===")
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    init_db(conn)

    mqtt_client = None
    if MQTT_ENABLED:
        try:
            log.info("Connecting to MQTT broker %s:%d…", MQTT_HOST, MQTT_PORT)
            mqtt_client = mqtt_connect()
            publish_discovery(mqtt_client)
        except Exception as e:
            log.warning("MQTT connection failed (%s) — continuing without HA.", e)
            mqtt_client = None

    first_run = True
    while True:
        try:
            run_sync(conn, mqtt_client, full_sync=first_run)
            first_run = False
        except requests.HTTPError as e:
            log.error("RWGPS API error: %s", e)
        except Exception as e:
            log.exception("Unexpected error during sync: %s", e)

        log.info("Sleeping %d minutes until next sync…", SYNC_INTERVAL // 60)
        time.sleep(SYNC_INTERVAL)


if __name__ == "__main__":
    main()
