import copy
import os
import sys
import tempfile
import threading

import pytest
from flask import Flask, jsonify, request, Response
from werkzeug.serving import make_server

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

# Kleine Nachbildung der TMDB- und Bücher-Schnittstellen
STATE = {"tv": {}, "movie": {}, "find": {}, "search": [], "google": {}, "dnb": "", "igdb": [], "jf": {}}
fake = Flask("fake")


def show(name, status="Returning Series", seasons=None, **kw):
    """seasons: {Staffel: [Ausstrahlungsdaten]}"""
    return {"name": name, "original_name": kw.get("original", name), "status": status, "overview": "Text", "poster_path": "/p.jpg",
            "backdrop_path": None, "first_air_date": "2020-01-01", "genres": [{"id": g} for g in kw.get("genres", [18])],
            "origin_country": kw.get("country", ["US"]), "original_language": "en",
            "external_ids": {"tvdb_id": kw.get("tvdb")}, "seasons_data": seasons or {}}


@fake.get("/3/tv/<int:i>")
def tv(i):
    s = STATE["tv"].get(i)
    if not s:
        return "", 404
    out = {k: v for k, v in s.items() if k != "seasons_data"}
    out["seasons"] = [{"season_number": n} for n in s["seasons_data"]]
    for part in request.args.get("append_to_response", "").split(","):
        if part.startswith("season/"):
            n = int(part.split("/")[1])
            out[part] = {"episodes": [{"episode_number": k + 1, "name": f"Folge {k + 1}", "air_date": d, "runtime": 42}
                                      for k, d in enumerate(s["seasons_data"][n])]}
    return jsonify(out)


@fake.get("/3/movie/<int:i>")
def movie(i):
    return jsonify(STATE["movie"][i])


@fake.get("/3/search/multi")
def search():
    return jsonify(results=STATE["search"])


@fake.get("/3/find/<i>")
def find(i):
    return jsonify(STATE["find"].get(i, {}))


@fake.get("/books/volumes")
def google():
    return jsonify(items=STATE["google"].get(request.args.get("q"), []))


@fake.get("/ol/search.json")
def ol():
    return jsonify(docs=[])


@fake.get("/dnb")
def dnb():
    return Response(STATE["dnb"], mimetype="application/xml")


@pytest.fixture(scope="session")
def server():
    srv = make_server("127.0.0.1", 0, fake, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture()
def env(server, monkeypatch, tmp_path):
    for k in ("tv", "movie", "find", "google"):
        STATE[k] = {}
    STATE["search"], STATE["dnb"], STATE["jf"] = [], "", {}
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TMDB_API_KEY", "testkey")
    monkeypatch.setenv("TMDB_BASE", server + "/3")
    monkeypatch.setenv("GOOGLE_BOOKS_BASE", server + "/books")
    monkeypatch.setenv("OPENLIBRARY_BASE", server + "/ol")
    monkeypatch.setenv("DNB_BASE", server + "/dnb")
    monkeypatch.setenv("JELLYFIN_URL", server + "/jf/")
    monkeypatch.setenv("JELLYFIN_API_KEY", "key")
    monkeypatch.setenv("IGDB_ID", "id")
    monkeypatch.setenv("IGDB_SECRET", "secret")
    monkeypatch.setenv("IGDB_AUTH", server + "/oauth")
    monkeypatch.setenv("IGDB_BASE", server + "/igdb")
    monkeypatch.setenv("FOLGENREICH_TODAY", "2026-06-01")
    monkeypatch.setenv("SYNC_PAUSE", "0")
    from folgenreich import create_app
    app = create_app(background=False)
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(env):
    c = env.test_client()
    c.post("/einrichten", data={"username": "nutzer1", "password": "geheim1234"})
    return c


def other(app, admin, name="nutzer2"):
    """Legt ein zweites Konto an und meldet es an."""
    r = admin.post("/konto/nutzer", data={"username": name}, follow_redirects=True)
    pw = r.get_data(as_text=True).split("Erstes Passwort: ")[1].split("<")[0]
    c = app.test_client()
    c.post("/anmelden", data={"username": name, "password": pw})
    return c


@fake.get("/jf/Users")
def jf_users():
    if "MediaBrowser Token" not in request.headers.get("Authorization", ""):
        return "", 401
    return jsonify([{"Name": "Nutzer1", "Id": "u1"}])


@fake.get("/jf/Items")
def jf_items():
    kind = request.args["IncludeItemTypes"]
    rows = STATE["jf"].get(kind, [])
    start, limit = int(request.args["StartIndex"]), int(request.args["Limit"])
    return jsonify(Items=rows[start:start + limit], TotalRecordCount=len(rows))


@fake.post("/oauth")
def oauth():
    return jsonify(access_token="tok", expires_in=3600)


@fake.post("/igdb/games")
def igdb_games():
    assert request.headers["Authorization"] == "Bearer tok"
    return jsonify(STATE["igdb"])
