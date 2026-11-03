"""Hält den Katalog aktuell. Laufende Serien alle 12 Stunden, beendete wöchentlich,
damit auch eine spätere Fortsetzung auffällt."""
import logging
import os
import threading
import time

from . import db, logic, tmdb

log = logging.getLogger("folgenreich")
_wake = threading.Event()
_started = False


def kick():
    _wake.set()


def get_or_create(type_, ext_id, title=None, poster=None):
    conn = db.get()
    row = conn.execute("SELECT id FROM media WHERE type=? AND ext_id=?", (type_, str(ext_id))).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO media(type,ext_id,source,title,poster) VALUES(?,?, 'tmdb',?,?)",
                       (type_, str(ext_id), title or f"TMDB {ext_id}", poster))
    conn.commit()
    return cur.lastrowid


def sync_media(media_id):
    conn = db.get()
    m = conn.execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
    if not m or m["source"] != "tmdb":
        return
    data = tmdb.tv(m["ext_id"]) if m["type"] == "tv" else tmdb.movie(m["ext_id"])
    eps = data.pop("episodes", None)
    anime = data.pop("anime", False)
    fields = {k: v for k, v in data.items() if v is not None or k not in ("title", "poster")}
    sets = ",".join(f"{k}=?" for k in fields)
    with conn:
        conn.execute(f"UPDATE media SET {sets}, synced_at=?, sync_error=NULL WHERE id=?",
                     [*fields.values(), logic.now(), media_id])
        if not m["anime_locked"]:
            conn.execute("UPDATE media SET anime=? WHERE id=?", (int(anime), media_id))
        if eps is not None:
            conn.execute("DELETE FROM episodes WHERE media_id=?", (media_id,))
            conn.executemany("INSERT OR REPLACE INTO episodes(media_id,season,number,title,air_date,runtime) "
                             "VALUES(?,?,?,?,?,?)", [(media_id, *e) for e in eps])


def due():
    return db.get().execute("""SELECT id FROM media WHERE source='tmdb' AND (
        synced_at IS NULL
        OR (status IN ('ended','canceled','released') AND synced_at<datetime('now','-7 days'))
        OR (status NOT IN ('ended','canceled','released') AND synced_at<datetime('now','-12 hours')))
        ORDER BY synced_at IS NOT NULL, synced_at LIMIT 400""").fetchall()


def run_once():
    """Arbeitet alles Fällige ab. Bei Ausfall von TMDB bricht die Runde ab und läuft später weiter."""
    done = 0
    for row in due():
        try:
            sync_media(row["id"])
            done += 1
        except tmdb.TmdbError as e:
            conn = db.get()
            conn.execute("UPDATE media SET sync_error=? WHERE id=?", (str(e), row["id"]))
            conn.commit()
            if "kennt diesen" not in str(e):
                log.warning("Abgleich unterbrochen: %s", e)
                break
        time.sleep(float(os.environ.get("SYNC_PAUSE", "0.15")))
    return done


def _loop():
    while True:
        try:
            if tmdb.configured():
                run_once()
        except Exception:  # der Hintergrund-Thread darf nie sterben
            log.exception("Abgleich fehlgeschlagen")
        _wake.wait(900)
        _wake.clear()


def start():
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_loop, daemon=True, name="sync").start()
