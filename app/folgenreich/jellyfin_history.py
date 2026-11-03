"""Übernimmt bereits Gesehenes aus Jellyfin.

Ablauf in zwei Schritten: Die Prüfung liest den Verlauf und ordnet ihn TMDB zu,
ändert aber noch keine Listen. Erst die Bestätigung trägt die Folgen ein.
Übernommen wird nur Zusätzliches: Nichts wird entfernt, und pausierte oder
abgebrochene Serien behalten ihren Status.

Jellyfin nutzt hier oft TheTVDB, TMDB zählt Staffeln manchmal anders. Passt eine
gesehene Folge nicht in die TMDB-Zählung, wird die ganze Serie Folge für Folge
über die TVDB-ID aufgelöst.
"""
import json
import logging
import os
import threading
from collections import defaultdict

import requests

from . import db, logic, sync, tmdb

log = logging.getLogger("folgenreich")
_running = set()
_guard = threading.Lock()


class JellyfinError(Exception):
    pass


def configured():
    return bool(os.environ.get("JELLYFIN_URL") and os.environ.get("JELLYFIN_API_KEY"))


def _get(path, **params):
    url = os.environ["JELLYFIN_URL"].rstrip("/") + path
    headers = {"Authorization": f'MediaBrowser Token="{os.environ["JELLYFIN_API_KEY"]}"'}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=60)
    except requests.RequestException:
        raise JellyfinError(f"Jellyfin ist unter {os.environ['JELLYFIN_URL']} nicht erreichbar.") from None
    if r.status_code in (401, 403):
        raise JellyfinError("Jellyfin lehnt den API-Schlüssel ab.")
    if not r.ok:
        raise JellyfinError(f"Jellyfin antwortet mit Fehler {r.status_code}.")
    return r.json()


def _items(user, kind, played=False):
    out, start = [], 0
    while True:
        params = dict(userId=user, IncludeItemTypes=kind, Recursive="true", Fields="ProviderIds",
                      StartIndex=start, Limit=500, enableUserData="true")
        if played:
            params["Filters"] = "IsPlayed"
        page = _get("/Items", **params)
        rows = page.get("Items") or []
        out += rows
        start += len(rows)
        if not rows or start >= page.get("TotalRecordCount", 0):
            return out


def _ids(item):
    return {k.lower(): v for k, v in (item.get("ProviderIds") or {}).items()}


def _user_id(name):
    for u in _get("/Users"):
        if u.get("Name", "").lower() == name.lower():
            return u["Id"]
    raise JellyfinError(f"In Jellyfin gibt es kein Konto „{name}“. Trag deinen Jellyfin-Namen oben ein.")


def _tv_id(series):
    """Sucht die TMDB-ID einer Jellyfin-Serie."""
    ids = _ids(series)
    if ids.get("tmdb"):
        return ids["tmdb"]
    for key, source in (("tvdb", "tvdb_id"), ("imdb", "imdb_id")):
        if ids.get(key):
            hits = tmdb.find(ids[key], source).get("tv_results") or []
            if hits:
                return hits[0]["id"]
    name = (series.get("Name") or "").lower()
    hits = [r for r in tmdb.search(series.get("Name") or "") if r["type"] == "tv" and (r["title"] or "").lower() == name]
    return hits[0]["ext_id"] if len(hits) == 1 else None


def _numbers(ep):
    if ep.get("ParentIndexNumber") is None or ep.get("IndexNumber") is None:
        return []
    return [(ep["ParentIndexNumber"], n) for n in range(ep["IndexNumber"], (ep.get("IndexNumberEnd") or ep["IndexNumber"]) + 1)]


def _resolve(ep):
    """Fragt TMDB nach der richtigen Zählung einer einzelnen Folge."""
    ids = _ids(ep)
    for key, source in (("tvdb", "tvdb_id"), ("imdb", "imdb_id")):
        if ids.get(key):
            hits = tmdb.find(ids[key], source).get("tv_episode_results") or []
            if hits:
                return hits[0]["season_number"], hits[0]["episode_number"]
    return None


def _map_series(media_id, eps):
    """Liefert ({(Staffel, Folge): Datum}, Zahl nicht zuordenbarer Folgen)."""
    known = {(r[0], r[1]) for r in db.get().execute("SELECT season,number FROM episodes WHERE media_id=?", (media_id,))}
    plain = [(k, e) for e in eps for k in _numbers(e)]
    if all(k in known for k, _ in plain):
        return {k: _played_at(e) for k, e in plain}, 0
    found, missed = {}, 0
    for e in eps:
        hit = _resolve(e)
        if hit and hit in known:
            found[hit] = _played_at(e)
        else:
            missed += 1
    return found, missed


def _played_at(item):
    stamp = (item.get("UserData") or {}).get("LastPlayedDate") or ""
    return stamp[:19].replace("T", " ") or None


def _path(user_id):
    return os.path.join(db.data_dir(), f"jellyfin-vorschau-{user_id}.json")


def _analyse(user_id, name):
    jf = _user_id(name)
    series = {s["Id"]: s for s in _items(jf, "Series")}
    by_series = defaultdict(list)
    for e in _items(jf, "Episode", played=True):
        by_series[e.get("SeriesId")].append(e)
    conn = db.get()
    plan, problems = [], []
    for sid, eps in by_series.items():
        info = series.get(sid) or {"Name": eps[0].get("SeriesName"), "ProviderIds": {}}
        title = info.get("Name") or "Unbekannte Serie"
        try:
            ext = _tv_id(info)
            if not ext:
                problems.append(f"{title}: nicht bei TMDB gefunden ({len(eps)} Folgen)")
                continue
            media_id = sync.get_or_create("tv", ext, title)
            if not conn.execute("SELECT synced_at FROM media WHERE id=?", (media_id,)).fetchone()[0]:
                sync.sync_media(media_id)
            mapped, missed = _map_series(media_id, eps)
        except tmdb.TmdbError as e:
            if "kennt diesen" in str(e):
                problems.append(f"{title}: {e}")
                continue
            raise
        if missed:
            problems.append(f"{title}: {missed} Folgen ließen sich nicht zuordnen")
        seen = {(r[0], r[1]) for r in conn.execute("SELECT season,number FROM watches WHERE user_id=? AND media_id=?",
                                                   (user_id, media_id))}
        fresh = [[s, n, at] for (s, n), at in sorted(mapped.items()) if (s, n) not in seen]
        if fresh:
            row = conn.execute("SELECT title FROM media WHERE id=?", (media_id,)).fetchone()
            listed = conn.execute("SELECT 1 FROM entries WHERE user_id=? AND media_id=?", (user_id, media_id)).fetchone()
            plan.append({"media_id": media_id, "title": row["title"], "new_entry": not listed, "episodes": fresh})
    movies = []
    for m in _items(jf, "Movie", played=True):
        ids = _ids(m)
        ext = ids.get("tmdb")
        try:
            if not ext and ids.get("imdb"):
                hits = tmdb.find(ids["imdb"], "imdb_id").get("movie_results") or []
                ext = hits[0]["id"] if hits else None
            if not ext:
                problems.append(f"{m.get('Name')}: Film nicht gefunden")
                continue
            media_id = sync.get_or_create("movie", ext, m.get("Name"))
            state = conn.execute("SELECT state FROM entries WHERE user_id=? AND media_id=?", (user_id, media_id)).fetchone()
            if state and state["state"] == "done":
                continue
            if not conn.execute("SELECT synced_at FROM media WHERE id=?", (media_id,)).fetchone()[0]:
                sync.sync_media(media_id)
        except tmdb.TmdbError as e:
            if "kennt diesen" in str(e):
                problems.append(f"{m.get('Name')}: {e}")
                continue
            raise
        movies.append({"media_id": media_id, "title": m.get("Name"), "at": _played_at(m)})
    plan.sort(key=lambda p: (-len(p["episodes"]), p["title"].lower()))
    return {"state": "ready", "series": plan, "movies": movies, "problems": problems,
            "episodes": sum(len(p["episodes"]) for p in plan), "new_entries": sum(p["new_entry"] for p in plan)}


def _save(user_id, data):
    tmp = _path(user_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, _path(user_id))


def status(user_id):
    with _guard:
        if user_id in _running:
            return {"state": "running"}
    try:
        with open(_path(user_id), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _work(user_id, name):
    try:
        _save(user_id, _analyse(user_id, name))
    except (JellyfinError, tmdb.TmdbError) as e:
        _save(user_id, {"state": "error", "error": str(e)})
    except Exception:
        log.exception("Jellyfin-Prüfung fehlgeschlagen")
        _save(user_id, {"state": "error", "error": "Die Prüfung ist unerwartet abgebrochen. Details stehen im Container-Log."})
    finally:
        with _guard:
            _running.discard(user_id)


def start(user_id, name, background=True):
    with _guard:
        if user_id in _running:
            return
        _running.add(user_id)
    if background:
        threading.Thread(target=_work, args=(user_id, name), daemon=True, name="jellyfin-pruefung").start()
    else:
        _work(user_id, name)


def discard(user_id):
    try:
        os.remove(_path(user_id))
    except OSError:
        pass


def apply(user_id):
    data = status(user_id)
    if not data or data.get("state") != "ready":
        return None
    conn = db.get()
    with conn:
        for s in data["series"]:
            logic.ensure_entry(user_id, s["media_id"], "follow")
            conn.executemany("INSERT OR IGNORE INTO watches(user_id,media_id,season,number,watched_at) VALUES(?,?,?,?,?)",
                             [(user_id, s["media_id"], e[0], e[1], e[2] or logic.now()) for e in s["episodes"]])
            conn.execute("""UPDATE entries SET hint=NULL, last_activity=MAX(last_activity,COALESCE(
                (SELECT MAX(watched_at) FROM watches w WHERE w.user_id=entries.user_id AND w.media_id=entries.media_id),''))
                WHERE user_id=? AND media_id=?""", (user_id, s["media_id"]))
            conn.execute("UPDATE entries SET state='follow' WHERE user_id=? AND media_id=? AND state='planned'",
                         (user_id, s["media_id"]))
        for m in data["movies"]:
            logic.ensure_entry(user_id, m["media_id"], "done")
            conn.execute("UPDATE entries SET state='done', finished_at=COALESCE(finished_at,?) WHERE user_id=? AND media_id=?",
                         (m["at"] or logic.now(), user_id, m["media_id"]))
    discard(user_id)
    return data
