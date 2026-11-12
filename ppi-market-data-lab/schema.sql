CREATE TABLE IF NOT EXISTS ppi_instruments (
  symbol TEXT PRIMARY KEY, underlying TEXT NOT NULL, expiry TEXT NOT NULL,
  type TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ppi_latest (
  symbol TEXT NOT NULL, underlying TEXT NOT NULL, expiry TEXT NOT NULL,
  type TEXT NOT NULL, settlement TEXT NOT NULL, observed_at TEXT NOT NULL,
  payload TEXT NOT NULL, PRIMARY KEY(symbol,settlement)
);
CREATE TABLE IF NOT EXISTS ppi_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
  underlying TEXT NOT NULL, expiry TEXT NOT NULL, type TEXT NOT NULL,
  settlement TEXT NOT NULL, observed_at TEXT NOT NULL, event_kind TEXT NOT NULL,
  payload TEXT NOT NULL, raw_message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ppi_events_lookup ON ppi_events(symbol,settlement,event_id DESC);
CREATE TABLE IF NOT EXISTS ppi_collector_state (
  id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capture_sessions (
  session_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
  status TEXT NOT NULL, config TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS connection_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
  observed_at TEXT NOT NULL, monotonic_ns INTEGER NOT NULL, state TEXT NOT NULL,
  detail TEXT
);
CREATE TABLE IF NOT EXISTS event_timing (
  event_id INTEGER PRIMARY KEY REFERENCES ppi_events(event_id), session_id TEXT NOT NULL,
  received_utc_ns INTEGER NOT NULL, received_monotonic_ns INTEGER NOT NULL,
  dequeued_monotonic_ns INTEGER NOT NULL, queue_delay_ns INTEGER NOT NULL,
  provider_timestamp TEXT NOT NULL, apparent_timestamp_lag_ms REAL,
  message_sha256 TEXT NOT NULL, out_of_order INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS timing_session ON event_timing(session_id,event_id);
