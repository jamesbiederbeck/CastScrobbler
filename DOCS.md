# CastScrobbler

*A Chromecast Telemetry Scraper*

Polls all mDNS-discoverable Chromecasts on a configurable interval and writes
media status snapshots to a local SQLite database at `/data/chromecast.db`.

---

## Networking

CastScrobbler uses `host_network: true` because Chromecast discovery relies on
Zeroconf/mDNS (multicast UDP on `224.0.0.251:5353`). Standard Docker bridge
networking drops this traffic by default.

**VLAN-segmented networks:** if your Chromecasts are on an IoT VLAN separate
from your Home Assistant host, you need either:

- An mDNS repeater (`avahi-daemon` with `allow-interfaces` spanning both VLANs,
  or a dedicated `mdns-repeater` container), **or**
- Static-IP polling — replace the `get_chromecasts()` call in
  `DiscoveryManager.discover()` in `scraper.py` with
  `pychromecast.get_listed_chromecasts(hosts=["x.x.x.x", ...])`.

---

## Configuration

All options are exposed in the Supervisor UI config panel.

| Option                    | Type          | Default | Description |
|---------------------------|---------------|---------|-------------|
| `poll_interval`           | int (≥10)     | `300`   | Seconds between poll cycles. |
| `discovery_timeout`       | int (5–60)    | `15`    | Seconds to wait for mDNS discovery per cycle. |
| `discover_every_n_polls`  | int (≥1)      | `12`    | Re-run mDNS discovery every N polls (default ≈ 1 h at 5 min interval). |
| `save_idle`               | bool          | `true`  | Write a row even when the device reports `IDLE`. |
| `save_unknown`            | bool          | `true`  | Write a row even when device state is `UNKNOWN`. |
| `only_on_change`          | bool          | `false` | Skip insert when the content fingerprint matches the previous row for that device. See note below. |
| `device_uuid_allowlist`   | string        | `""`    | Comma-separated Chromecast UUIDs to poll. Leave blank to poll all discovered devices. |
| `device_name_regex`       | string        | `""`    | Python regex matched against device friendly name. Leave blank for all devices. |
| `log_level`               | debug/info/warning/error | `info` | Verbosity of container logs. Use `debug` when diagnosing discovery issues. |

### `only_on_change` tradeoff

**`false` (default):** every poll writes a row — good for time-series
reconstruction and understanding how long something was playing.

**`true`:** inserts only when the content fingerprint changes — far fewer rows,
but you lose granularity. You can still infer session length from the gap
between the last matching row and the next different one.

The fingerprint covers: `app_id`, `state`, `title`, `series`, `season`,
`episode`, `content_id`, `content_type`, `artist`, `album`.

---

## Database

The database is written to `/data/chromecast.db` inside the add-on container.
The Supervisor mounts `/data` writable for every add-on automatically.

To access the database file from outside the add-on, you can use the
**File editor** or **SSH & Web Terminal** add-on to browse to
`/addon_configs/castscrobbler/` or copy it to `/share/` with a one-liner:

```bash
cp /data/chromecast.db /share/chromecast.db
```

Then retrieve `/share/chromecast.db` via Samba or another file-sharing add-on.

---

## Schema

```
plays (
    id                  INTEGER  PK
    ts                  TEXT     ISO-8601 UTC timestamp
    ts_unix             INTEGER  Unix epoch seconds (use this for range queries)
    device_name         TEXT
    device_uuid         TEXT
    app_id              TEXT     Internal Cast app identifier
    app_name            TEXT     Human name, e.g. "YouTube", "Netflix", "Spotify"
    state               TEXT     PLAYING / PAUSED / BUFFERING / IDLE / UNKNOWN
    title               TEXT
    series              TEXT
    season              INTEGER
    episode             INTEGER
    artist              TEXT
    album               TEXT
    content_id          TEXT     URL or opaque ID depending on sender app
    content_type        TEXT     MIME type, e.g. "video/mp4"
    stream_type         TEXT     BUFFERED / LIVE / NONE
    duration_s          REAL
    current_s           REAL     Playback position at time of snapshot
    playback_rate       REAL
    idle_reason         TEXT     FINISHED / CANCELLED / INTERRUPTED / ERROR
    volume_level        REAL     0.0–1.0
    volume_muted        INTEGER  0 or 1
    is_active_input     INTEGER  0 or 1 (HDMI-CEC active input signal)
    media_session_id    TEXT
    images_json         TEXT     JSON array of artwork URLs
    content_fingerprint TEXT     SHA-1 of key media fields (for dedup/change detection)
    raw_json            TEXT     Forward-compat snapshot of all fields
)
```

**Indexes:** `ts_unix`, `(device_uuid, ts_unix)`, `(content_id, ts_unix)`, `(title, ts_unix)`.

---

## Useful queries

```sql
-- Recent activity, excluding idle
SELECT datetime(ts_unix, 'unixepoch') AS time, device_name, app_name, state, title, artist
FROM plays
WHERE state NOT IN ('IDLE', 'UNKNOWN')
ORDER BY ts_unix DESC
LIMIT 50;

-- Watch/listen history by day and device (deduplicated by fingerprint)
SELECT date(ts_unix, 'unixepoch') AS day,
       device_name,
       app_name,
       title,
       artist,
       COUNT(DISTINCT content_fingerprint) AS distinct_items
FROM plays
WHERE title IS NOT NULL OR artist IS NOT NULL
GROUP BY day, device_name, app_name, title, artist
ORDER BY day DESC;

-- Current state of each device (most recent snapshot per device)
SELECT device_name, app_name, state, title, artist,
       current_s, duration_s,
       datetime(ts_unix, 'unixepoch') AS last_seen
FROM plays
WHERE ts_unix = (
    SELECT MAX(ts_unix) FROM plays p2 WHERE p2.device_uuid = plays.device_uuid
)
ORDER BY device_name;
```
