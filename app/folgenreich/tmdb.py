"""Zugriff auf TMDB: Suche, Serien, Filme, externe IDs."""
import os
import time

import requests

IMG = "https://image.tmdb.org/t/p/"
STATUS = {"Returning Series": "running", "In Production": "running", "Planned": "planned", "Pilot": "planned",
          "Ended": "ended", "Canceled": "canceled", "Released": "released", "Post Production": "planned",
          "Rumored": "planned"}


class TmdbError(Exception):
    pass


def key():
    return (os.environ.get("TMDB_API_KEY") or os.environ.get("TMDB_API") or "").strip()


def configured():
    return bool(key())


def _get(path, **params):
    k = key()
    if not k:
        raise TmdbError("Es ist kein TMDB-Schlüssel eingetragen.")
    headers = {"Accept": "application/json"}
    if len(k) > 40:  # Lese-Token (v4)
        headers["Authorization"] = "Bearer " + k
    else:
        params["api_key"] = k
    base = os.environ.get("TMDB_BASE", "https://api.themoviedb.org/3")
    for attempt in range(4):
        try:
            r = requests.get(base + path, params=params, headers=headers, timeout=20)
        except requests.RequestException as e:
            if attempt == 3:
                raise TmdbError(f"TMDB ist nicht erreichbar: {type(e).__name__}") from None
            time.sleep(1 + attempt)
            continue
        if r.status_code == 429:
            time.sleep(float(r.headers.get("Retry-After", 2)))
            continue
        if r.status_code == 401:
            raise TmdbError("TMDB lehnt den Schlüssel ab.")
        if r.status_code == 404:
            raise TmdbError("TMDB kennt diesen Eintrag nicht.")
        if not r.ok:
            raise TmdbError(f"TMDB antwortet mit Fehler {r.status_code}.")
        return r.json()
    raise TmdbError("TMDB drosselt gerade die Anfragen.")


def img(path, size="w342"):
    return IMG + size + path if path else None


def search(query):
    data = _get("/search/multi", query=query, language="de-DE", include_adult="false")
    out = []
    for r in data.get("results", []):
        if r.get("media_type") not in ("tv", "movie"):
            continue
        d = r.get("first_air_date") or r.get("release_date") or ""
        out.append({"type": r["media_type"], "ext_id": str(r["id"]), "title": r.get("name") or r.get("title"),
                    "year": d[:4] or None, "poster": img(r.get("poster_path")), "overview": r.get("overview") or "",
                    "anime": is_anime(r.get("genre_ids") or [], r.get("origin_country") or [],
                                      r.get("original_language"))})
    return out


def is_anime(genre_ids, countries, lang):
    return 16 in genre_ids and ("JP" in countries or lang == "ja")


def find(external_id, source):
    """source: tvdb_id oder imdb_id"""
    return _get(f"/find/{external_id}", external_source=source, language="de-DE")


def tv(tmdb_id):
    """Serie samt aller Staffeln und Folgen. TMDB erlaubt 20 Anhänge pro Anfrage."""
    data = _get(f"/tv/{tmdb_id}", language="de-DE", append_to_response="external_ids")
    if not data.get("overview"):
        data["overview"] = _get(f"/tv/{tmdb_id}", language="en-US").get("overview")
    numbers = [s["season_number"] for s in data.get("seasons", [])]
    episodes = []
    for i in range(0, len(numbers), 20):
        chunk = numbers[i:i + 20]
        part = _get(f"/tv/{tmdb_id}", language="de-DE",
                    append_to_response=",".join(f"season/{n}" for n in chunk))
        for n in chunk:
            for e in (part.get(f"season/{n}") or {}).get("episodes", []):
                episodes.append((n, e["episode_number"], e.get("name"), e.get("air_date") or None, e.get("runtime")))
    ids = data.get("external_ids") or {}
    genres = [g["id"] for g in data.get("genres", [])]
    d = data.get("first_air_date") or ""
    return {"title": data.get("name"), "original_title": data.get("original_name"), "year": int(d[:4]) if d[:4] else None,
            "poster": img(data.get("poster_path")), "backdrop": img(data.get("backdrop_path"), "w780"),
            "overview": data.get("overview"), "status": STATUS.get(data.get("status"), "unknown"),
            "release_date": d or None, "tvdb_id": str(ids["tvdb_id"]) if ids.get("tvdb_id") else None,
            "imdb_id": ids.get("imdb_id"),
            "anime": is_anime(genres, data.get("origin_country") or [], data.get("original_language")),
            "episodes": episodes}


def movie(tmdb_id):
    data = _get(f"/movie/{tmdb_id}", language="de-DE")
    if not data.get("overview"):
        data["overview"] = _get(f"/movie/{tmdb_id}", language="en-US").get("overview")
    d = data.get("release_date") or ""
    return {"title": data.get("title"), "original_title": data.get("original_title"),
            "year": int(d[:4]) if d[:4] else None, "poster": img(data.get("poster_path")),
            "backdrop": img(data.get("backdrop_path"), "w780"), "overview": data.get("overview"),
            "status": STATUS.get(data.get("status"), "unknown"), "release_date": d or None,
            "runtime": data.get("runtime"), "imdb_id": data.get("imdb_id")}
