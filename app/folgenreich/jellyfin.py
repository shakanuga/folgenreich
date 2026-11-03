"""Automatisches Abhaken über das Jellyfin-Webhook-Plugin.

Wichtig: Bei Folgen liefert das Plugin die IDs der FOLGE, nicht der Serie. Die
TVDB- oder IMDb-Folgen-ID lässt sich aber über TMDB /find auflösen und liefert
Serie, Staffel und Folge in TMDB-Zählung. So stören abweichende Staffelzählungen
zwischen TheTVDB und TMDB nicht. Klappt das nicht, hilft der Serienname.
"""
from . import db, logic, sync, tmdb


def _log(ok, text):
    conn = db.get()
    conn.execute("INSERT INTO hook_log(ok,text) VALUES(?,?)", (int(ok), text[:300]))
    conn.execute("DELETE FROM hook_log WHERE id NOT IN (SELECT id FROM hook_log ORDER BY id DESC LIMIT 50)")
    conn.commit()


def _user(name):
    conn = db.get()
    return conn.execute("SELECT id FROM users WHERE jellyfin_name=? COLLATE NOCASE "
                        "OR (jellyfin_name IS NULL AND username=? COLLATE NOCASE)", (name, name)).fetchone()


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _via_tmdb_find(p):
    for key, source in (("Provider_tvdb", "tvdb_id"), ("Provider_imdb", "imdb_id")):
        if p.get(key):
            try:
                eps = tmdb.find(p[key], source).get("tv_episode_results") or []
            except tmdb.TmdbError:
                continue
            if eps:
                e = eps[0]
                return e["show_id"], e["season_number"], e["episode_number"]
    return None


def _by_name(user_id, name):
    conn = db.get()
    row = conn.execute("SELECT media_id FROM hook_alias WHERE name=?", (name,)).fetchone()
    if row:
        return row["media_id"]
    rows = conn.execute("""SELECT m.id FROM media m JOIN entries e ON e.media_id=m.id AND e.user_id=?
        WHERE m.type='tv' AND (m.title=? COLLATE NOCASE OR m.original_title=? COLLATE NOCASE)""",
                        (user_id, name, name)).fetchall()
    return rows[0]["id"] if len(rows) == 1 else None


def handle(p):
    if p.get("NotificationType") != "PlaybackStop":
        return "ignored"
    if str(p.get("PlayedToCompletion")).lower() != "true":
        return "ignored"
    name = p.get("NotificationUsername") or p.get("Username") or ""
    user = _user(name)
    if not user:
        return "ignored"  # z. B. Freunde, die nur Jellyfin nutzen
    uid = user["id"]
    if p.get("ItemType") == "Movie":
        return _movie(uid, p)
    if p.get("ItemType") == "Episode":
        return _episode(uid, p)
    return "ignored"


def _episode(uid, p):
    series = p.get("SeriesName") or ""
    season = _int(p.get("SeasonNumber") if p.get("SeasonNumber") is not None else p.get("SeasonNumber00"))
    number = _int(p.get("EpisodeNumber") if p.get("EpisodeNumber") is not None else p.get("EpisodeNumber00"))
    label = f"{series} S{season}E{number}"
    media_id = None
    hit = _via_tmdb_find(p)
    if hit:
        show, season, number = hit
        media_id = sync.get_or_create("tv", show, series)
        if not db.get().execute("SELECT synced_at FROM media WHERE id=?", (media_id,)).fetchone()[0]:
            try:
                sync.sync_media(media_id)
            except tmdb.TmdbError:
                pass
    else:
        media_id = _by_name(uid, series)
    if not media_id or season is None or number is None:
        _log(False, f"{label}: keine passende Serie gefunden")
        return "unmatched"
    logic.set_watched(uid, media_id, [(season, number)])
    conn = db.get()
    conn.execute("INSERT OR REPLACE INTO hook_alias(name,media_id) VALUES(?,?)", (series, media_id))
    conn.commit()
    _log(True, f"{label} für {p.get('NotificationUsername')} abgehakt")
    return "ok"


def _movie(uid, p):
    name = p.get("Name") or ""
    conn = db.get()
    ext = p.get("Provider_tmdb")
    if not ext and p.get("Provider_imdb"):
        try:
            res = tmdb.find(p["Provider_imdb"], "imdb_id").get("movie_results") or []
            ext = res[0]["id"] if res else None
        except tmdb.TmdbError:
            ext = None
    if not ext:
        _log(False, f"{name}: Film nicht gefunden")
        return "unmatched"
    media_id = sync.get_or_create("movie", ext, name)
    if not conn.execute("SELECT synced_at FROM media WHERE id=?", (media_id,)).fetchone()[0]:
        try:
            sync.sync_media(media_id)
        except tmdb.TmdbError:
            pass
    logic.set_state(uid, media_id, "done")
    _log(True, f"{name} für {p.get('NotificationUsername')} als gesehen eingetragen")
    return "ok"
