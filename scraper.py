#!/usr/bin/env python3
"""
Chromecast Now-Playing Scraper

Architecture:
  - One long-lived discovery session (re-runs every DISCOVER_EVERY_N_POLLS cycles)
  - Status read from cached pychromecast state; no sleep() sync hacks
  - One transaction per poll cycle (executemany)
  - Optional change-only mode via content fingerprint
  - Graceful shutdown on SIGTERM/SIGINT
"""

import hashlib
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional

import pychromecast

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH               = os.environ.get("DB_PATH", "/data/chromecast.db")
POLL_INTERVAL         = int(os.environ.get("POLL_INTERVAL", "300"))
DISCOVERY_TIMEOUT     = int(os.environ.get("DISCOVERY_TIMEOUT", "15"))
DISCOVER_EVERY_N      = int(os.environ.get("DISCOVER_EVERY_N_POLLS", "12"))  # ~1h at 5min default
SAVE_IDLE             = os.environ.get("SAVE_IDLE", "true").lower() == "true"
SAVE_UNKNOWN          = os.environ.get("SAVE_UNKNOWN", "true").lower() == "true"
ONLY_ON_CHANGE        = os.environ.get("ONLY_ON_CHANGE", "false").lower() == "true"
DEVICE_UUID_ALLOWLIST = set(filter(None, os.environ.get("DEVICE_UUID_ALLOWLIST", "").split(",")))
DEVICE_NAME_REGEX     = os.environ.get("DEVICE_NAME_REGEX", "")
LOG_LEVEL             = os.environ.get("LOG_LEVEL", "INFO").upper()
SUPERVISOR_TOKEN      = os.environ.get("SUPERVISOR_TOKEN", "")
PUBLISH_HA_STATES     = os.environ.get("PUBLISH_HA_STATES", "true").lower() == "true"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS plays (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT    NOT NULL,
    ts_unix             INTEGER NOT NULL,
    device_name         TEXT    NOT NULL,
    device_uuid         TEXT,
    app_id              TEXT,
    app_name            TEXT,
    state               TEXT,
    title               TEXT,
    series              TEXT,
    season              INTEGER,
    episode             INTEGER,
    artist              TEXT,
    album               TEXT,
    content_id          TEXT,
    content_type        TEXT,
    stream_type         TEXT,
    duration_s          REAL,
    current_s           REAL,
    playback_rate       REAL,
    idle_reason         TEXT,
    volume_level        REAL,
    volume_muted        INTEGER,
    is_active_input     INTEGER,
    media_session_id    TEXT,
    images_json         TEXT,
    content_fingerprint TEXT,
    raw_json            TEXT
);

CREATE INDEX IF NOT EXISTS idx_plays_ts_unix       ON plays(ts_unix);
CREATE INDEX IF NOT EXISTS idx_plays_device_ts     ON plays(device_uuid, ts_unix);
CREATE INDEX IF NOT EXISTS idx_plays_content_id_ts ON plays(content_id, ts_unix);
CREATE INDEX IF NOT EXISTS idx_plays_title_ts      ON plays(title, ts_unix);
"""

INSERT_SQL = """
INSERT INTO plays (
    ts, ts_unix, device_name, device_uuid,
    app_id, app_name, state,
    title, series, season, episode,
    artist, album, content_id, content_type, stream_type,
    duration_s, current_s, playback_rate, idle_reason,
    volume_level, volume_muted, is_active_input, media_session_id,
    images_json, content_fingerprint, raw_json
) VALUES (
    :ts, :ts_unix, :device_name, :device_uuid,
    :app_id, :app_name, :state,
    :title, :series, :season, :episode,
    :artist, :album, :content_id, :content_type, :stream_type,
    :duration_s, :current_s, :playback_rate, :idle_reason,
    :volume_level, :volume_muted, :is_active_input, :media_session_id,
    :images_json, :content_fingerprint, :raw_json
)
"""


def init_db(path: str) -> sqlite3.Connection:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        PRAGMA busy_timeout=5000;
    """)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def last_fingerprint(conn: sqlite3.Connection, device_uuid: Optional[str]) -> Optional[str]:
    if not device_uuid:
        return None
    row = conn.execute(
        "SELECT content_fingerprint FROM plays WHERE device_uuid=? ORDER BY ts_unix DESC LIMIT 1",
        (device_uuid,)
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Device filtering
# ---------------------------------------------------------------------------

def _compile_name_regex():
    if not DEVICE_NAME_REGEX:
        return None
    return re.compile(DEVICE_NAME_REGEX)

_name_re = _compile_name_regex()


def device_allowed(cast) -> bool:
    if DEVICE_UUID_ALLOWLIST and str(cast.uuid) not in DEVICE_UUID_ALLOWLIST:
        return False
    if _name_re and not _name_re.search(cast.name):
        return False
    return True


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

FINGERPRINT_FIELDS = (
    "app_id", "state", "title", "series", "season", "episode",
    "content_id", "content_type", "artist", "album",
)


def make_fingerprint(rec: dict) -> str:
    blob = "|".join(str(rec.get(f) or "") for f in FINGERPRINT_FIELDS)
    return hashlib.sha1(blob.encode()).hexdigest()


def snapshot_cast(cast) -> Optional[dict]:
    # wait() returns False on timeout (does NOT raise) — treat as unreachable
    if not cast.wait(timeout=5):
        log.warning("Timed out connecting to %s, skipping", cast.name)
        return None

    ms = cast.media_controller.status  # cached — no sleep() sync hack
    cs = cast.status                   # CastStatus: volume, active input, app

    now = datetime.now(timezone.utc)
    images = [img.url for img in ms.images if img.url] if (ms and ms.images) else []

    rec = {
        "ts":               now.isoformat(),
        "ts_unix":          int(now.timestamp()),
        "device_name":      cast.name,
        "device_uuid":      str(cast.uuid) if cast.uuid else None,
        "app_id":           cs.app_id if cs else None,
        "app_name":         cs.display_name if cs else None,
        "state":            ms.player_state if ms else "UNKNOWN",
        "title":            ms.title if ms else None,
        "series":           ms.series_title if ms else None,
        "season":           ms.season if ms else None,
        "episode":          ms.episode if ms else None,
        "artist":           ms.artist if ms else None,
        "album":            ms.album_name if ms else None,
        "content_id":       ms.content_id if ms else None,
        "content_type":     ms.content_type if ms else None,
        "stream_type":      ms.stream_type if ms else None,
        "duration_s":       ms.duration if ms else None,
        "current_s":        ms.current_time if ms else None,
        "playback_rate":    ms.playback_rate if ms else None,
        "idle_reason":      ms.idle_reason if ms else None,
        "volume_level":     cs.volume_level if cs else None,
        "volume_muted":     int(cs.volume_muted) if cs and cs.volume_muted is not None else None,
        "is_active_input":  int(cs.is_active_input) if cs and cs.is_active_input is not None else None,
        "media_session_id": str(ms.media_session_id) if ms and ms.media_session_id else None,
        "images_json":      json.dumps(images) if images else None,
    }

    rec["content_fingerprint"] = make_fingerprint(rec)
    raw = {k: v for k, v in rec.items() if k not in ("images_json", "raw_json")}
    raw["images"] = images
    rec["raw_json"] = json.dumps(raw)

    return rec


# ---------------------------------------------------------------------------
# Home Assistant state publisher
# ---------------------------------------------------------------------------

_HA_STATE_MAP = {
    "PLAYING":   "playing",
    "PAUSED":    "paused",
    "BUFFERING": "buffering",
    "IDLE":      "idle",
}


def _entity_id(device_name: str) -> str:
    """Return a stable HA entity ID for a Chromecast device name."""
    slug = re.sub(r"[^a-z0-9]+", "_", device_name.lower()).strip("_")
    return f"media_player.castscrobbler_{slug}"


def _ha_post(entity_id: str, payload: dict, label: str):
    """POST a state payload to the Home Assistant REST API."""
    url = f"http://supervisor/core/api/states/{entity_id}"
    data = json.dumps(payload).encode()
    headers = {
        "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.debug(
                "HA state published for %s → %s (HTTP %d)",
                label, entity_id, resp.status,
            )
    except urllib.error.HTTPError as e:
        log.warning("Failed to publish HA state for %s: HTTP %d", label, e.code)
    except Exception as e:
        log.error("Error publishing HA state for %s: %s", label, e)


def publish_to_ha(rec: dict):
    """Publish a snapshot record as a media_player entity state in Home Assistant."""
    if not SUPERVISOR_TOKEN or not PUBLISH_HA_STATES:
        return

    ha_state = _HA_STATE_MAP.get(rec.get("state"), "standby")

    volume_muted_raw = rec.get("volume_muted")
    attrs = {
        "friendly_name":      rec["device_name"],
        "source":             "CastScrobbler",
        "device_uuid":        rec.get("device_uuid"),
        "app_id":             rec.get("app_id"),
        "app_name":           rec.get("app_name"),
        "media_content_id":   rec.get("content_id"),
        "media_content_type": rec.get("content_type"),
        "media_title":        rec.get("title"),
        "media_series_title": rec.get("series"),
        "media_season":       rec.get("season"),
        "media_episode":      rec.get("episode"),
        "media_artist":       rec.get("artist"),
        "media_album_name":   rec.get("album"),
        "media_duration":     rec.get("duration_s"),
        "media_position":     rec.get("current_s"),
        "playback_rate":      rec.get("playback_rate"),
        "volume_level":       rec.get("volume_level"),
        "is_volume_muted":    bool(volume_muted_raw) if volume_muted_raw is not None else None,
        "is_active_input":    rec.get("is_active_input"),
        "media_session_id":   rec.get("media_session_id"),
        "idle_reason":        rec.get("idle_reason"),
        "stream_type":        rec.get("stream_type"),
    }

    if rec.get("images_json"):
        try:
            images = json.loads(rec["images_json"])
            if images:
                attrs["entity_picture"] = images[0]
        except Exception:
            pass

    # Keep False/0 values; only drop None
    payload_attrs = {k: v for k, v in attrs.items() if v is not None}

    _ha_post(
        _entity_id(rec["device_name"]),
        {"state": ha_state, "attributes": payload_attrs},
        rec["device_name"],
    )


def mark_unavailable_in_ha(cast):
    """Mark a Chromecast as unavailable in Home Assistant (e.g. on connection timeout)."""
    if not SUPERVISOR_TOKEN or not PUBLISH_HA_STATES:
        return

    attrs = {"friendly_name": cast.name, "source": "CastScrobbler"}
    if cast.uuid:
        attrs["device_uuid"] = str(cast.uuid)

    _ha_post(
        _entity_id(cast.name),
        {"state": "unavailable", "attributes": attrs},
        cast.name,
    )


# ---------------------------------------------------------------------------
# Discovery manager
# ---------------------------------------------------------------------------

class DiscoveryManager:
    def __init__(self):
        self.chromecasts: list = []
        self.browser = None

    def discover(self):
        self._stop()
        log.info("Running mDNS discovery (timeout=%ds)…", DISCOVERY_TIMEOUT)
        self.chromecasts, self.browser = pychromecast.get_chromecasts(timeout=DISCOVERY_TIMEOUT)
        allowed = [c for c in self.chromecasts if device_allowed(c)]
        log.info("Discovered %d device(s), %d after filtering", len(self.chromecasts), len(allowed))
        self.chromecasts = allowed

    def _stop(self):
        if self.browser:
            try:
                pychromecast.stop_discovery(self.browser)
            except Exception:
                pass
            self.browser = None

    def disconnect_all(self):
        for cast in self.chromecasts:
            try:
                cast.disconnect(timeout=3, blocking=False)
            except Exception:
                pass
        self._stop()


# ---------------------------------------------------------------------------
# Poll cycle
# ---------------------------------------------------------------------------

def poll_cycle(conn: sqlite3.Connection, discovery: DiscoveryManager):
    records = []

    for cast in discovery.chromecasts:
        try:
            rec = snapshot_cast(cast)

            if rec is None:
                mark_unavailable_in_ha(cast)
                continue

            publish_to_ha(rec)  # always push to HA, before any DB-level filters

            state = rec.get("state")
            if not SAVE_IDLE and state == "IDLE":
                log.debug("Skipping idle device %s", cast.name)
                continue
            if not SAVE_UNKNOWN and state == "UNKNOWN":
                log.debug("Skipping unknown state on %s", cast.name)
                continue

            if ONLY_ON_CHANGE:
                prev = last_fingerprint(conn, rec.get("device_uuid"))
                if prev == rec["content_fingerprint"]:
                    log.debug("No change on %s, skipping insert", cast.name)
                    continue

            records.append(rec)
            log.info(
                "  [%s] app=%s state=%s title=%s",
                cast.name,
                rec.get("app_name") or rec.get("app_id") or "—",
                state,
                rec.get("title") or rec.get("artist") or "—",
            )

        except Exception as e:
            log.error("Error polling %s: %s", cast.name, e, exc_info=True)

    if records:
        with conn:  # single transaction, one fsync
            conn.executemany(INSERT_SQL, records)
        log.info("Inserted %d record(s)", len(records))
    else:
        log.info("Nothing to insert this cycle")


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

_shutdown = False


def _handle_signal(sig, frame):
    global _shutdown
    log.info("Received signal %d, shutting down…", sig)
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info(
        "Starting. DB=%s  interval=%ds  only_on_change=%s  save_idle=%s  save_unknown=%s  publish_ha_states=%s",
        DB_PATH, POLL_INTERVAL, ONLY_ON_CHANGE, SAVE_IDLE, SAVE_UNKNOWN, PUBLISH_HA_STATES,
    )
    conn = init_db(DB_PATH)
    discovery = DiscoveryManager()
    poll_count = 0

    try:
        while not _shutdown:
            if poll_count % DISCOVER_EVERY_N == 0:
                try:
                    discovery.discover()
                except Exception as e:
                    log.error("Discovery failed: %s", e, exc_info=True)

            if discovery.chromecasts:
                try:
                    poll_cycle(conn, discovery)
                except Exception as e:
                    log.error("Poll cycle failed: %s", e, exc_info=True)
            else:
                log.warning("No devices available, skipping poll")

            poll_count += 1

            # Interruptible sleep: check _shutdown every second
            for _ in range(POLL_INTERVAL):
                if _shutdown:
                    break
                time.sleep(1)

    finally:
        log.info("Cleaning up…")
        discovery.disconnect_all()
        conn.close()
        log.info("Done.")
        sys.exit(0)


if __name__ == "__main__":
    main()
