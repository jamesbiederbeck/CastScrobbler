#!/usr/bin/with-contenv bashio

export DB_PATH="/data/chromecast.db"
export POLL_INTERVAL="$(bashio::config 'poll_interval')"
export DISCOVERY_TIMEOUT="$(bashio::config 'discovery_timeout')"
export DISCOVER_EVERY_N_POLLS="$(bashio::config 'discover_every_n_polls')"
export SAVE_IDLE="$(bashio::config 'save_idle')"
export SAVE_UNKNOWN="$(bashio::config 'save_unknown')"
export ONLY_ON_CHANGE="$(bashio::config 'only_on_change')"
export DEVICE_UUID_ALLOWLIST="$(bashio::config 'device_uuid_allowlist')"
export DEVICE_NAME_REGEX="$(bashio::config 'device_name_regex')"
export LOG_LEVEL="$(bashio::config 'log_level' | tr '[:lower:]' '[:upper:]')"

exec python3 -u /app/scraper.py
