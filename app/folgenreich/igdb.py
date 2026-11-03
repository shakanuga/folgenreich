"""Spielesuche über IGDB. Zugang über eine kostenlose Twitch-App (IGDB_ID, IGDB_SECRET).
Ohne Zugang funktioniert alles außer der Suche: Spiele lassen sich dann von Hand eintragen."""
import os
import threading
import time

import requests

_lock = threading.Lock()
_token = {"value": None, "until": 0}


class IgdbError(Exception):
    pass


def configured():
    return bool(os.environ.get("IGDB_ID") and os.environ.get("IGDB_SECRET"))


def _access():
    with _lock:
        if _token["value"] and _token["until"] > time.time() + 60:
            return _token["value"]
        url = os.environ.get("IGDB_AUTH", "https://id.twitch.tv/oauth2/token")
        try:
            r = requests.post(url, params={"client_id": os.environ["IGDB_ID"], "client_secret": os.environ["IGDB_SECRET"],
                                           "grant_type": "client_credentials"}, timeout=15)
        except requests.RequestException:
            raise IgdbError("Die Spieledatenbank ist nicht erreichbar.") from None
        if not r.ok:
            raise IgdbError("Die Spieledatenbank lehnt die Zugangsdaten ab.")
        data = r.json()
        _token.update(value=data["access_token"], until=time.time() + data.get("expires_in", 3600))
        return _token["value"]


def search(query):
    if not configured():
        raise IgdbError("Für die Spielesuche fehlen IGDB_ID und IGDB_SECRET.")
    safe = query.replace('"', " ").replace("\\", " ")
    body = (f'search "{safe}"; fields name,first_release_date,cover.image_id,platforms.name,genres.name,summary; '
            'where version_parent = null; limit 20;')
    base = os.environ.get("IGDB_BASE", "https://api.igdb.com/v4")
    try:
        r = requests.post(base + "/games", data=body.encode(), timeout=15,
                          headers={"Client-ID": os.environ["IGDB_ID"], "Authorization": "Bearer " + _access()})
    except requests.RequestException:
        raise IgdbError("Die Spieledatenbank ist nicht erreichbar.") from None
    if not r.ok:
        raise IgdbError(f"Die Spieledatenbank antwortet mit Fehler {r.status_code}.")
    out = []
    for g in r.json():
        cover = (g.get("cover") or {}).get("image_id")
        stamp = g.get("first_release_date")
        out.append({"ext_id": str(g["id"]), "title": g.get("name"),
                    "year": time.gmtime(stamp).tm_year if stamp else None,
                    "cover": f"https://images.igdb.com/igdb/image/upload/t_cover_big/{cover}.jpg" if cover else None,
                    "platforms": [p["name"] for p in g.get("platforms") or []],
                    "genres": [x["name"] for x in g.get("genres") or []], "overview": g.get("summary")})
    return [g for g in out if g["title"]]
