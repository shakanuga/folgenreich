"""Übernimmt einen Yamtrack-CSV-Export. Mehrfaches Importieren schadet nicht."""
import csv
import io
from collections import Counter, defaultdict

from . import db, sync

STATE = {"In progress": "follow", "Completed": "follow", "Paused": "paused", "Dropped": "dropped",
         "Planning": "planned", "Repeating": "follow"}


def _stamp(value):
    return (value or "").replace("T", " ")[:19] or None


def _num(value):
    return float(value) if value not in (None, "") else None


def run(text, user_id):
    conn = db.get()
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows or "media_type" not in rows[0]:
        raise ValueError("Das sieht nicht nach einem Yamtrack-Export aus.")
    res = Counter()
    skipped = Counter()
    season_scores = defaultdict(list)
    plays = Counter()
    first = {}
    for r in rows:
        if r["source"] != "tmdb" or r["media_type"] not in ("tv", "season", "episode", "movie"):
            skipped[r["media_type"]] += 1
            continue
        if r["media_type"] == "season" and r["score"]:
            season_scores[r["media_id"]].append(f"Staffel {int(float(r['season_number']))}: {float(r['score']):g}/10")
        if r["media_type"] == "episode":
            k = (r["media_id"], int(float(r["season_number"])), int(float(r["episode_number"])))
            plays[k] += 1
            at = _stamp(r["end_date"]) or _stamp(r["created_at"])
            first[k] = min(first.get(k, at), at)
    with conn:
        for r in rows:
            if r["source"] != "tmdb" or r["media_type"] not in ("tv", "movie"):
                continue
            poster = r["image"] if r["image"].startswith("http") else None
            mid = sync.get_or_create(r["media_type"], r["media_id"], r["title"], poster)
            if r["media_type"] == "tv":
                state, hint = STATE.get(r["status"], "follow"), r["status"]
                res["serien"] += 1
            else:
                state, hint = ("done" if r["status"] == "Completed" else "planned"), None
                res["filme"] += 1
            activity = _stamp(r["progressed_at"]) or _stamp(r["end_date"]) or _stamp(r["created_at"])
            notes = "\n".join(x for x in [r["notes"], "Bewertung aus Yamtrack: " + ", ".join(season_scores[r["media_id"]])
                                          if season_scores.get(r["media_id"]) else ""] if x) or None
            conn.execute("""INSERT INTO entries(user_id,media_id,state,rating,notes,hint,added_at,last_activity,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,media_id) DO NOTHING""",
                         (user_id, mid, state, _num(r["score"]), notes, hint, _stamp(r["created_at"]), activity,
                          _stamp(r["end_date"]) if state == "done" else None))
        ids = {r[0]: r[1] for r in conn.execute("SELECT ext_id,id FROM media WHERE type='tv'")}
        for (ext, s, n), count in plays.items():
            if ext not in ids:
                skipped["folge ohne serie"] += 1
                continue
            conn.execute("""INSERT INTO watches(user_id,media_id,season,number,watched_at,plays) VALUES(?,?,?,?,?,?)
                ON CONFLICT(user_id,media_id,season,number) DO UPDATE SET plays=MAX(plays,excluded.plays)""",
                         (user_id, ids[ext], s, n, first[(ext, s, n)], count))
            res["folgen"] += 1
        # letzte Aktivität einer Serie = zuletzt gesehene Folge
        conn.execute("""UPDATE entries SET last_activity=COALESCE((SELECT MAX(watched_at) FROM watches w
            WHERE w.user_id=entries.user_id AND w.media_id=entries.media_id), last_activity) WHERE user_id=?""",
                     (user_id,))
    sync.kick()
    res["mehrfach"] = sum(c - 1 for c in plays.values())
    return dict(res), dict(skipped)
