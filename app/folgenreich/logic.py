"""Herzstück der App: Wo gehört eine Serie gerade hin?

Aktiv, Warten und Archiv werden nicht gespeichert, sondern bei jedem Aufruf aus
dem Fortschritt und den TMDB-Daten berechnet. Dadurch wandert eine archivierte
Serie von selbst zurück, sobald neue Folgen da sind oder eine neue Staffel
angekündigt wird.

Als offen gilt jede erschienene Folge NACH der zuletzt gesehenen. Lücken davor
(z. B. nur Staffel 3 und 4 gesehen) halten eine Serie nicht in Aktiv fest.
Specials (Staffel 0) zählen nie zum Fortschritt.
"""
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db

POS = 100000  # season*POS+number ergibt eine sortierbare Folgen-Position


def tz():
    return ZoneInfo(os.environ.get("TZ") or "Europe/Berlin")


def today():
    forced = os.environ.get("FOLGENREICH_TODAY")  # nur für Tests
    return forced or datetime.now(tz()).date().isoformat()


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def pos_label(pos):
    return f"S{pos // POS:02d}E{pos % POS:02d}"


def classify(state, hint, synced, status, open_eps, watched, last_air, next_air, archived_at):
    """Liefert (Reiter, Unterart) für eine Serie."""
    if state == "dropped":
        return "archiv", "dropped"
    if state == "paused":
        return "aktiv", "paused"
    if state == "planned" and not watched:
        return "merkliste", None
    if not synced:
        return ("archiv", "finished") if hint == "Completed" else ("aktiv", None)
    if state == "archived":
        if not last_air or last_air[:10] <= (archived_at or "")[:10]:
            return "archiv", "finished"
    if open_eps > 0:
        return "aktiv", None
    if status in ("ended", "canceled") and not next_air:
        return "archiv", "finished"
    return "warten", None


TV_SQL = """
WITH pos AS (
  SELECT media_id, MAX(season*:p+number) AS last_pos, COUNT(*) AS watched
  FROM watches WHERE user_id=:u AND season>0 GROUP BY media_id)
SELECT m.*, e.state, e.hint, e.rating, e.notes, e.archived_at, e.added_at, e.last_activity, e.owned,
       COALESCE(pos.watched,0) AS watched, COALESCE(pos.last_pos,0) AS last_pos,
       (SELECT COUNT(*) FROM episodes x WHERE x.media_id=m.id AND x.season>0
          AND x.air_date<=:t) AS aired,
       (SELECT COUNT(*) FROM episodes x WHERE x.media_id=m.id AND x.season>0
          AND x.air_date<=:t AND x.season*:p+x.number>COALESCE(pos.last_pos,0)) AS open_eps,
       (SELECT MIN(x.season*:p+x.number) FROM episodes x WHERE x.media_id=m.id AND x.season>0
          AND x.air_date<=:t AND x.season*:p+x.number>COALESCE(pos.last_pos,0)) AS next_pos,
       (SELECT MAX(x.air_date) FROM episodes x WHERE x.media_id=m.id AND x.season>0
          AND x.air_date<=:t) AS last_air,
       (SELECT MIN(x.air_date) FROM episodes x WHERE x.media_id=m.id AND x.season>0
          AND x.air_date>:t) AS next_air
FROM entries e JOIN media m ON m.id=e.media_id
LEFT JOIN pos ON pos.media_id=m.id
WHERE e.user_id=:u AND m.type='tv' {extra}
"""


def tv_items(user_id, media_id=None):
    extra = "AND m.id=:m" if media_id else ""
    rows = db.get().execute(TV_SQL.format(extra=extra),
                            {"p": POS, "u": user_id, "t": today(), "m": media_id}).fetchall()
    items = []
    for r in rows:
        it = dict(r)
        it["tab"], it["sub"] = classify(it["state"], it["hint"], bool(it["synced_at"]), it["status"], it["open_eps"],
                                        it["watched"], it["last_air"], it["next_air"], it["archived_at"])
        it["pct"] = round(100 * it["watched"] / it["aired"]) if it["aired"] else 0
        items.append(it)
    return items


def attach_next(items):
    """Ergänzt nächste Folge und Folgen-Streifen der aktuellen Staffel."""
    conn = db.get()
    for it in items:
        it["next_title"], it["strip"], it["next_season"] = None, [], None
        if not it.get("next_pos"):
            continue
        s, n = it["next_pos"] // POS, it["next_pos"] % POS
        it["next_season"], it["next_num"] = s, n
        rows = conn.execute("SELECT number,title,air_date FROM episodes WHERE media_id=? AND season=? ORDER BY number",
                            (it["id"], s)).fetchall()
        seen = {r[0] for r in conn.execute("SELECT number FROM watches WHERE user_id=? AND media_id=? AND season=?",
                                           (it["user_id"], it["id"], s))}
        t = today()
        for r in rows:
            cls = "w" if r["number"] in seen else ("n" if r["number"] == n else
                                                   ("o" if r["air_date"] and r["air_date"] <= t else "f"))
            it["strip"].append(cls)
            if r["number"] == n:
                it["next_title"] = r["title"]
    return items


def waiting_label(it):
    if it.get("next_air"):
        return "Nächste Folge am " + de_date(it["next_air"])
    return {"running": "Noch kein Termin für neue Folgen", "planned": "Neue Staffel ist geplant",
            "ended": "Beendet", "canceled": "Abgesetzt"}.get(it["status"], "Aktuell nichts Neues")


def de_date(iso):
    try:
        return date.fromisoformat(iso[:10]).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return iso or ""


def tv_tabs(user_id, anime):
    items = [i for i in tv_items(user_id) if bool(i["anime"]) == anime]
    for i in items:
        i["user_id"] = user_id
    tabs = {"aktiv": [], "warten": [], "archiv": [], "merkliste": []}
    for i in items:
        tabs[i["tab"]].append(i)
    fresh_after = (date.fromisoformat(today()) - timedelta(days=14)).isoformat()
    attach_next(tabs["aktiv"])
    for i in tabs["aktiv"]:
        i["fresh"] = bool(i["last_air"] and i["last_air"] >= fresh_after and i["sub"] != "paused")
    tabs["aktiv"].sort(key=lambda i: (i["sub"] == "paused", not i["fresh"], _rev(i["last_activity"])))
    tabs["warten"].sort(key=lambda i: (i["next_air"] or "9999", i["title"].lower()))
    tabs["archiv"].sort(key=lambda i: _rev(i["last_activity"]))
    tabs["merkliste"].sort(key=lambda i: _rev(i["added_at"]))
    return tabs


def _rev(s):
    """Sortierschlüssel für absteigende Zeitstempel."""
    return [-ord(c) for c in s or ""]


def plain_items(user_id, type_):
    rows = db.get().execute("""SELECT m.*, e.state, e.rating, e.notes, e.owned, e.added_at, e.last_activity, e.finished_at
        FROM entries e JOIN media m ON m.id=e.media_id WHERE e.user_id=? AND m.type=?
        ORDER BY e.last_activity DESC""", (user_id, type_)).fetchall()
    return [dict(r) for r in rows]


def touch(user_id, media_id):
    db.get().execute("UPDATE entries SET last_activity=? WHERE user_id=? AND media_id=?", (now(), user_id, media_id))


def ensure_entry(user_id, media_id, state, **fields):
    """Legt einen Eintrag an. Vorhandene Einträge bleiben unverändert."""
    conn = db.get()
    cols = ["user_id", "media_id", "state"] + list(fields)
    vals = [user_id, media_id, state] + list(fields.values())
    marks = ",".join("?" * len(vals))
    conn.execute(f"INSERT OR IGNORE INTO entries({','.join(cols)}) VALUES({marks})", vals)


def _wake(user_id, media_id):
    """Wer eine Folge abhakt, schaut die Serie wieder."""
    db.get().execute("""UPDATE entries SET state='follow', archived_at=NULL, hint=NULL
                        WHERE user_id=? AND media_id=? AND state IN ('planned','paused','dropped','archived')""",
                     (user_id, media_id))
    ensure_entry(user_id, media_id, "follow")


def set_watched(user_id, media_id, episodes, watched=True, at=None):
    """episodes: Liste aus (Staffel, Folge)."""
    conn = db.get()
    if watched:
        _wake(user_id, media_id)
        conn.executemany("INSERT OR IGNORE INTO watches(user_id,media_id,season,number,watched_at) VALUES(?,?,?,?,?)",
                         [(user_id, media_id, s, n, at or now()) for s, n in episodes])
    else:
        conn.executemany("DELETE FROM watches WHERE user_id=? AND media_id=? AND season=? AND number=?",
                         [(user_id, media_id, s, n) for s, n in episodes])
    touch(user_id, media_id)
    conn.commit()


def aired_episodes(media_id, season=None, upto=None):
    sql = "SELECT season,number FROM episodes WHERE media_id=? AND air_date<=?"
    args = [media_id, today()]
    if season is not None:
        sql += " AND season=?"
        args.append(season)
    else:
        sql += " AND season>0"
    if upto is not None:
        sql += " AND season*?+number<=?"
        args += [POS, upto]
    return [(r[0], r[1]) for r in db.get().execute(sql, args)]


def mark_next(user_id, media_id):
    item = tv_items(user_id, media_id)
    if not item or not item[0]["next_pos"]:
        return None
    p = item[0]["next_pos"]
    set_watched(user_id, media_id, [(p // POS, p % POS)])
    return p


def set_state(user_id, media_id, state):
    conn = db.get()
    archived_at = now() if state == "archived" else None
    finished = now() if state == "done" else None
    conn.execute("""INSERT INTO entries(user_id,media_id,state,archived_at,finished_at) VALUES(?,?,?,?,?)
                    ON CONFLICT(user_id,media_id) DO UPDATE SET state=excluded.state, hint=NULL,
                    archived_at=excluded.archived_at,
                    finished_at=COALESCE(excluded.finished_at, entries.finished_at)""",
                 (user_id, media_id, state, archived_at, finished))
    touch(user_id, media_id)
    conn.commit()


def remove_entry(user_id, media_id):
    conn = db.get()
    conn.execute("DELETE FROM entries WHERE user_id=? AND media_id=?", (user_id, media_id))
    conn.execute("DELETE FROM watches WHERE user_id=? AND media_id=?", (user_id, media_id))
    conn.commit()


def seasons(user_id, media_id):
    conn = db.get()
    seen = {(r[0], r[1]): r[2] for r in conn.execute(
        "SELECT season,number,plays FROM watches WHERE user_id=? AND media_id=?", (user_id, media_id))}
    t = today()
    out = {}
    for r in conn.execute("SELECT * FROM episodes WHERE media_id=? ORDER BY season=0, season, number", (media_id,)):
        s = out.setdefault(r["season"], {"number": r["season"], "eps": [], "seen": 0, "aired": 0})
        aired = bool(r["air_date"] and r["air_date"] <= t)
        ok = (r["season"], r["number"]) in seen
        s["eps"].append({"number": r["number"], "title": r["title"], "air": de_date(r["air_date"]) if r["air_date"] else "",
                         "aired": aired, "seen": ok, "plays": seen.get((r["season"], r["number"]), 0),
                         "pos": r["season"] * POS + r["number"]})
        s["seen"] += ok
        s["aired"] += aired
    # Gesehene Folgen ohne TMDB-Daten (z. B. vor dem ersten Abgleich) trotzdem zeigen
    for (sn, num), plays in sorted(seen.items()):
        s = out.setdefault(sn, {"number": sn, "eps": [], "seen": 0, "aired": 0})
        if not any(e["number"] == num for e in s["eps"]):
            s["eps"].append({"number": num, "title": None, "air": "", "aired": True, "seen": True, "plays": plays,
                             "pos": sn * POS + num})
            s["seen"] += 1
            s["aired"] += 1
    return sorted(out.values(), key=lambda s: (s["number"] == 0, s["number"]))


def upcoming(user_id, days=14):
    t = today()
    until = (date.fromisoformat(t) + timedelta(days=days)).isoformat()
    return [dict(r) for r in db.get().execute("""
        SELECT m.id, m.title, m.poster, x.season, x.number, x.title AS ep_title, MIN(x.air_date) AS air_date
        FROM episodes x JOIN media m ON m.id=x.media_id JOIN entries e ON e.media_id=m.id AND e.user_id=?
        WHERE x.season>0 AND x.air_date>? AND x.air_date<=? AND e.state NOT IN ('dropped','paused')
        GROUP BY m.id ORDER BY air_date, m.title LIMIT 12""", (user_id, t, until))]


def human_tv_status(it):
    if it["tab"] == "archiv":
        return "Abgebrochen" if it["sub"] == "dropped" else "Fertig gesehen"
    if it["tab"] == "warten":
        return waiting_label(it)
    if it["tab"] == "merkliste":
        return "Auf der Merkliste"
    return "Pausiert" if it["sub"] == "paused" else f"{it['open_eps']} offen" if it["open_eps"] else "Aktiv"


# --- Spiele --------------------------------------------------------------------------------------

FORMATS = ["Disc", "Modul", "Kassette", "Diskette", "Download-Code", "Digital"]
CONDITIONS = ["Versiegelt", "Sehr gut", "Gut", "Gebraucht", "Defekt"]
PLATFORMS = ["PlayStation 5", "PlayStation 4", "PlayStation 3", "PlayStation 2", "PlayStation", "PS Vita", "PSP",
             "Xbox Series X|S", "Xbox One", "Xbox 360", "Xbox", "Nintendo Switch 2", "Nintendo Switch", "Wii U", "Wii",
             "Nintendo 3DS", "Nintendo DS", "GameCube", "Nintendo 64", "SNES", "NES", "Game Boy Advance",
             "Game Boy Color", "Game Boy", "Sega Dreamcast", "Sega Mega Drive", "PC"]
GAME_STATE = {"planned": "Auf der Wunschliste", "backlog": "Noch nicht gespielt", "playing": "Spiele ich gerade",
              "done": "Durchgespielt", "dropped": "Abgebrochen"}


def game_items(user_id):
    rows = db.get().execute("""SELECT m.*, e.state, e.rating, e.notes, e.added_at, e.last_activity, e.finished_at,
          (SELECT COUNT(*) FROM copies c WHERE c.user_id=e.user_id AND c.media_id=m.id) AS n_copies,
          (SELECT GROUP_CONCAT(platform,'|') FROM (SELECT DISTINCT platform FROM copies c
             WHERE c.user_id=e.user_id AND c.media_id=m.id)) AS platforms
        FROM entries e JOIN media m ON m.id=e.media_id WHERE e.user_id=? AND m.type='game'
        ORDER BY m.title COLLATE NOCASE""", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def copies(user_id, media_id):
    return [dict(r) for r in db.get().execute("SELECT * FROM copies WHERE user_id=? AND media_id=? ORDER BY platform,id",
                                              (user_id, media_id))]


def known_platforms(user_id):
    mine = [r[0] for r in db.get().execute("SELECT DISTINCT platform FROM copies WHERE user_id=? ORDER BY 1", (user_id,))]
    return mine + [p for p in PLATFORMS if p not in mine]


def parse_price(text):
    """'29,99', '29.99' oder '30' werden zu Cent."""
    t = (text or "").replace("€", "").replace(" ", "").strip()
    if not t:
        return None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        return max(0, round(float(t) * 100))
    except ValueError:
        return None


def save_copy(user_id, media_id, form, copy_id=None):
    fields = (form.get("platform", "").strip()[:60], form.get("format") or None, form.get("edition", "").strip() or None,
              form.get("condition") or None, 1 if form.get("complete") else 0, parse_price(form.get("price")),
              form.get("bought_on") or None, form.get("location", "").strip() or None)
    if not fields[0]:
        return False
    conn = db.get()
    if copy_id:
        conn.execute("""UPDATE copies SET platform=?,format=?,edition=?,condition=?,complete=?,price_cents=?,bought_on=?,location=?
                        WHERE id=? AND user_id=?""", (*fields, copy_id, user_id))
    else:
        conn.execute("""INSERT INTO copies(platform,format,edition,condition,complete,price_cents,bought_on,location,user_id,media_id)
                        VALUES(?,?,?,?,?,?,?,?,?,?)""", (*fields, user_id, media_id))
        conn.execute("UPDATE entries SET state='backlog' WHERE user_id=? AND media_id=? AND state='planned'",
                     (user_id, media_id))
    touch(user_id, media_id)
    conn.commit()
    return True


def _bars(pairs, limit=None):
    """Sortiert nach Wert und fasst den Rest unter Sonstige zusammen."""
    items = sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0]))
    if limit and len(items) > limit:
        rest = sum(v for _, v in items[limit - 1:])
        items = items[:limit - 1] + [("Sonstige", rest)]
    top = max((v for _, v in items), default=1) or 1
    return [{"label": k, "value": v, "pct": round(100 * v / top, 1)} for k, v in items]


def game_stats(user_id):
    conn = db.get()
    rows = conn.execute("""SELECT c.*, m.title, m.genres, m.year, e.state FROM copies c JOIN media m ON m.id=c.media_id
        JOIN entries e ON e.user_id=c.user_id AND e.media_id=c.media_id WHERE c.user_id=?""", (user_id,)).fetchall()
    games = {r["media_id"]: r for r in rows}
    count = lambda key: _tally(rows, key)
    by_platform, spent = {}, {}
    for r in rows:
        by_platform[r["platform"]] = by_platform.get(r["platform"], 0) + 1
        spent[r["platform"]] = spent.get(r["platform"], 0) + (r["price_cents"] or 0)
    genres = {}
    for g in games.values():
        for name in filter(None, (g["genres"] or "").split(", ")):
            genres[name] = genres.get(name, 0) + 1
    decades, bought = {}, {}
    for g in games.values():
        if g["year"]:
            d = f"{g['year'] // 10 * 10}er"
            decades[d] = decades.get(d, 0) + 1
    for r in rows:
        if r["bought_on"]:
            bought[r["bought_on"][:4]] = bought.get(r["bought_on"][:4], 0) + 1
    states = {}
    for g in games.values():
        states[g["state"]] = states.get(g["state"], 0) + 1
    priced = [r for r in rows if r["price_cents"] is not None]
    total = sum(r["price_cents"] for r in priced)
    platforms = _bars(by_platform)
    for p in platforms:
        p["spent"] = spent.get(p["label"], 0)
    return {"copies": len(rows), "games": len(games), "physical": sum(r["format"] != "Digital" for r in rows),
            "platforms": platforms, "spent": total, "priced": len(priced),
            "average": round(total / len(priced)) if priced else 0,
            "done": states.get("done", 0), "unplayed": states.get("backlog", 0),
            "done_pct": round(100 * states.get("done", 0) / len(games)) if games else 0,
            "genres": _bars(genres, 8), "decades": sorted(_bars(decades), key=lambda b: b["label"]),
            "bought": sorted(_bars(bought), key=lambda b: b["label"]),
            "formats": _bars(count("format")), "conditions": _bars(count("condition")),
            "states": _bars({GAME_STATE.get(k, k): v for k, v in states.items()}),
            "complete": sum(r["complete"] for r in rows),
            "priciest": sorted(priced, key=lambda r: -r["price_cents"])[:5]}


def _tally(rows, key):
    out = {}
    for r in rows:
        if r[key]:
            out[r[key]] = out.get(r[key], 0) + 1
    return out
