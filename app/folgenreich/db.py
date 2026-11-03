"""SQLite-Datenbank. Eine Datei unter DATA_DIR, keine externe Datenbank nötig."""
import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  pw_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  jellyfin_name TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Katalog: gilt für alle Nutzer gemeinsam
CREATE TABLE IF NOT EXISTS media (
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL,              -- tv | movie | book | game
  ext_id TEXT NOT NULL,            -- TMDB-ID, ISBN oder interne ID
  source TEXT NOT NULL,            -- tmdb | books | manual
  anime INTEGER NOT NULL DEFAULT 0,
  anime_locked INTEGER NOT NULL DEFAULT 0,
  title TEXT NOT NULL,
  original_title TEXT,
  year INTEGER,
  poster TEXT,
  backdrop TEXT,
  overview TEXT,
  status TEXT NOT NULL DEFAULT 'unknown',  -- running | planned | ended | canceled | released | unknown
  release_date TEXT,
  runtime INTEGER,
  tvdb_id TEXT,
  imdb_id TEXT,
  authors TEXT,
  publisher TEXT,
  pages INTEGER,
  genres TEXT,
  synced_at TEXT,
  sync_error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (type, ext_id)
);
CREATE TABLE IF NOT EXISTS episodes (
  media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
  season INTEGER NOT NULL,
  number INTEGER NOT NULL,
  title TEXT,
  air_date TEXT,
  runtime INTEGER,
  PRIMARY KEY (media_id, season, number)
) WITHOUT ROWID;
-- Eintrag eines Nutzers. Aktiv/Warten/Archiv werden daraus berechnet (logic.py).
CREATE TABLE IF NOT EXISTS entries (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
  state TEXT NOT NULL,   -- follow | planned | paused | dropped | archived | reading | done
  owned INTEGER NOT NULL DEFAULT 0,
  rating REAL,
  notes TEXT,
  hint TEXT,             -- Yamtrack-Status, gilt bis zum ersten Abgleich
  archived_at TEXT,
  finished_at TEXT,
  added_at TEXT NOT NULL DEFAULT (datetime('now')),
  last_activity TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (user_id, media_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS watches (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
  season INTEGER NOT NULL,
  number INTEGER NOT NULL,
  watched_at TEXT NOT NULL DEFAULT (datetime('now')),
  plays INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (user_id, media_id, season, number)
) WITHOUT ROWID;
-- Physische Exemplare von Spielen: dasselbe Spiel kann auf mehreren Plattformen im Regal stehen
CREATE TABLE IF NOT EXISTS copies (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
  platform TEXT NOT NULL,
  format TEXT,
  edition TEXT,
  condition TEXT,
  complete INTEGER NOT NULL DEFAULT 0,
  price_cents INTEGER,
  bought_on TEXT,
  location TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_copies_user ON copies(user_id, media_id);
-- Gelernte Strichcodes von Spielen, gilt für alle Konten
CREATE TABLE IF NOT EXISTS ean_map (
  ean TEXT PRIMARY KEY,
  media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
  platform TEXT
);
CREATE TABLE IF NOT EXISTS hook_alias (
  name TEXT PRIMARY KEY COLLATE NOCASE,
  media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS hook_log (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL DEFAULT (datetime('now')),
  ok INTEGER NOT NULL,
  text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_watches_user ON watches(user_id, media_id);
CREATE INDEX IF NOT EXISTS idx_media_title ON media(title COLLATE NOCASE);
"""

COLUMNS = [("media", "genres", "TEXT")]

_local = threading.local()


def data_dir():
    return os.environ.get("DATA_DIR", "/data")


def path():
    return os.path.join(data_dir(), "folgenreich.db")


def connect():
    conn = sqlite3.connect(path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def get():
    """Eine Verbindung pro Thread. So teilen sich Anfragen keine Transaktion."""
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != path():
        conn = connect()
        _local.conn, _local.path = conn, path()
    return conn


def init():
    os.makedirs(data_dir(), exist_ok=True)
    os.makedirs(os.path.join(data_dir(), "covers"), exist_ok=True)
    conn = connect()
    conn.executescript(SCHEMA)
    # Neue Spalten für bestehende Datenbanken: hier ergänzen, nie umbenennen
    for table, column, kind in COLUMNS:
        if column not in [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    conn.commit()
    conn.close()


def setting(key, default=None):
    row = get().execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def set_setting(key, value):
    conn = get()
    conn.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
    conn.commit()
