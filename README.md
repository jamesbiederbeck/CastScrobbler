# chromecast-scraper

Polls all mDNS-discoverable Chromecasts on a configurable interval and writes
media status snapshots to a local SQLite database.

## Quick start

```bash
docker compose up -d --build
docker compose logs -f
```

Data lands in `./data/chromecast.db`.

---

## Configuration

All options are env vars, set in `docker-compose.yml` or passed via `-e`.

| Variable                | Default               | Description                                                                 |
|-------------------------|-----------------------|-----------------------------------------------------------------------------|
| `DB_PATH`               | `/data/chromecast.db` | SQLite path inside the container                                            |
| `POLL_INTERVAL`         | `300`                 | Seconds between poll cycles                                                 |
| `DISCOVERY_TIMEOUT`     | `15`                  | Seconds to wait for mDNS discovery                                          |
| `DISCOVER_EVERY_N_POLLS`| `12`                  | Re-run mDNS discovery every N polls (default ≈ 1h at 5min interval)        |
| `SAVE_IDLE`             | `true`                | Write rows when device state is `IDLE`                                      |
| `SAVE_UNKNOWN`          | `true`                | Write rows when device state is `UNKNOWN`                                   |
| `ONLY_ON_CHANGE`        | `false`               | Skip insert if content fingerprint matches the previous row for that device |
| `DEVICE_UUID_ALLOWLIST` | *(all)*               | Comma-separated UUIDs; if set, only these devices are polled                |
| `DEVICE_NAME_REGEX`     | *(all)*               | Python regex matched against device friendly name                           |
| `LOG_LEVEL`             | `INFO`                | Python log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`)                      |
| `PUBLISH_HA_STATES`     | `true`                | Publish each Chromecast as a `media_player` entity in Home Assistant (add-on mode only; requires `SUPERVISOR_TOKEN`) |

### `ONLY_ON_CHANGE` tradeoff

`false` (default): every poll writes a row — good for time-series reconstruction
and understanding how long something was playing.

`true`: inserts only when the content fingerprint changes — far fewer rows, but
you lose granularity. You can still infer session length from the gap between the
last matching row and the next different one.

The fingerprint covers: `app_id`, `state`, `title`, `series`, `season`,
`episode`, `content_id`, `content_type`, `artist`, `album`.

---

### Home Assistant device states

When running as a Home Assistant add-on, CastScrobbler automatically publishes
each discovered Chromecast as a `media_player` entity (e.g.
`media_player.castscrobbler_living_room_tv`) after every poll cycle. The entity
state mirrors the Chromecast playback state:

| Chromecast state | HA entity state |
|------------------|-----------------|
| `PLAYING`        | `playing`       |
| `PAUSED`         | `paused`        |
| `BUFFERING`      | `buffering`     |
| `IDLE`           | `idle`          |
| other / unknown  | `standby`       |
| connection timeout | `unavailable` |

Entity attributes include `media_title`, `media_artist`, `media_album_name`,
`app_name`, `volume_level`, `is_volume_muted`, `media_duration`,
`media_position`, and more — enough to drive automations and dashboards.

This feature requires the `SUPERVISOR_TOKEN` environment variable, which is
injected automatically in add-on mode. Set `PUBLISH_HA_STATES=false` to disable
it if you only need the SQLite database.

---

## Networking

The container uses `network_mode: host` because Chromecast discovery relies on
Zeroconf/mDNS (multicast UDP on `224.0.0.251:5353`). Docker bridge networking
drops this traffic by default.

**VLAN-segmented networks:** if your Chromecasts are on an IoT VLAN, you need
either:

- An mDNS repeater (`avahi-daemon` with `allow-interfaces` spanning both VLANs,
  or a dedicated `mdns-repeater` container), **or**
- Static-IP polling — replace the `get_chromecasts()` call in `DiscoveryManager.discover()`
  with:

  ```python
  self.chromecasts, self.browser = pychromecast.get_listed_chromecasts(
      hosts=["192.168.10.5", "192.168.10.6"]
  )
  ```

  This bypasses mDNS entirely and is more reliable if your DHCP reservations are stable.

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

-- Most-played content
SELECT title, artist, app_name, COUNT(*) AS snapshots
FROM plays
WHERE title IS NOT NULL
GROUP BY title, artist, app_name
ORDER BY snapshots DESC
LIMIT 20;

-- Current state of each device (most recent snapshot per device)
SELECT device_name, app_name, state, title, artist,
       current_s, duration_s,
       datetime(ts_unix, 'unixepoch') AS last_seen
FROM plays
WHERE ts_unix = (
    SELECT MAX(ts_unix) FROM plays p2 WHERE p2.device_uuid = plays.device_uuid
)
ORDER BY device_name;

-- Approximate session lengths: time between first and last snapshot per content fingerprint
SELECT device_name,
       app_name,
       title,
       artist,
       content_fingerprint,
       datetime(MIN(ts_unix), 'unixepoch') AS started,
       datetime(MAX(ts_unix), 'unixepoch') AS last_seen,
       ROUND((MAX(ts_unix) - MIN(ts_unix)) / 60.0, 1) AS duration_mins
FROM plays
WHERE content_fingerprint IS NOT NULL
  AND title IS NOT NULL
GROUP BY device_uuid, content_fingerprint
ORDER BY MIN(ts_unix) DESC
LIMIT 30;

-- Devices that went idle after playing (useful for detecting watch completion)
SELECT p1.device_name,
       p1.title,
       p1.idle_reason,
       datetime(p1.ts_unix, 'unixepoch') AS idle_at
FROM plays p1
WHERE p1.state = 'IDLE'
  AND p1.idle_reason IS NOT NULL
ORDER BY p1.ts_unix DESC
LIMIT 20;
```
