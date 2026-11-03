import pytest

from conftest import STATE, other, show
from folgenreich import db, jellyfin_history as jh, logic, sync


def ep(series, s, n, tvdb=None, played="2025-03-02T20:15:00.0000000Z", end=None):
    return {"SeriesId": series, "SeriesName": series, "ParentIndexNumber": s, "IndexNumber": n, "IndexNumberEnd": end,
            "ProviderIds": {"Tvdb": tvdb} if tvdb else {}, "UserData": {"LastPlayedDate": played}}


def check(client):
    assert client.post("/konto/jellyfin/pruefen").status_code == 302
    return jh.status(1)


def watches(ext="100"):
    return sorted(tuple(r) for r in db.get().execute(
        "SELECT season,number FROM watches w JOIN media m ON m.id=w.media_id WHERE m.ext_id=?", (ext,)))


def test_preview_changes_nothing_then_apply_adds(client):
    STATE["tv"][100] = show("Die Serie", seasons={1: ["2020-01-01"] * 3})
    STATE["jf"] = {"Series": [{"Id": "a", "Name": "Die Serie", "ProviderIds": {"Tmdb": "100"}}],
                   "Episode": [ep("a", 1, 1), ep("a", 1, 2)]}
    d = check(client)
    assert d["episodes"] == 2 and d["new_entries"] == 1
    assert not watches() and not logic.tv_items(1)
    assert "2 Folgen in 1 Serien" in client.get("/konto").get_data(as_text=True)
    client.post("/konto/jellyfin/uebernehmen")
    assert watches() == [(1, 1), (1, 2)]
    item = logic.tv_items(1)[0]
    assert item["tab"] == "aktiv" and logic.pos_label(item["next_pos"]) == "S01E03"
    assert db.get().execute("SELECT watched_at FROM watches").fetchone()[0] == "2025-03-02 20:15:00"
    assert jh.status(1) is None


def test_only_missing_episodes_and_nothing_removed(client):
    STATE["tv"][100] = show("S", seasons={1: ["2020-01-01"] * 3})
    client.post("/hinzufuegen", data={"type": "tv", "ext_id": 100, "action": "schaue"})
    m = db.get().execute("SELECT id FROM media").fetchone()[0]
    logic.set_watched(1, m, [(1, 3)])
    STATE["jf"] = {"Series": [{"Id": "a", "Name": "S", "ProviderIds": {"Tmdb": "100"}}], "Episode": [ep("a", 1, 1)]}
    assert check(client)["episodes"] == 1
    client.post("/konto/jellyfin/uebernehmen")
    assert watches() == [(1, 1), (1, 3)]


def test_dropped_show_stays_dropped(client):
    STATE["tv"][100] = show("S", seasons={1: ["2020-01-01"] * 3})
    client.post("/hinzufuegen", data={"type": "tv", "ext_id": 100, "action": "schaue"})
    m = db.get().execute("SELECT id FROM media").fetchone()[0]
    client.post(f"/m/{m}/status", data={"state": "dropped"})
    STATE["jf"] = {"Series": [{"Id": "a", "Name": "S", "ProviderIds": {"Tmdb": "100"}}], "Episode": [ep("a", 1, 1)]}
    check(client)
    client.post("/konto/jellyfin/uebernehmen")
    assert logic.tv_items(1)[0]["sub"] == "dropped"


def test_tvdb_numbering_differs_from_tmdb(client):
    # TheTVDB: Staffel 2 Folge 1. TMDB: alles in Staffel 1, Folge 13.
    STATE["tv"][100] = show("Anime", seasons={1: ["2020-01-01"] * 24}, tvdb=900)
    STATE["find"]["900"] = {"tv_results": [{"id": 100}]}
    STATE["find"]["e1"] = {"tv_episode_results": [{"season_number": 1, "episode_number": 13}]}
    STATE["jf"] = {"Series": [{"Id": "a", "Name": "Anime", "ProviderIds": {"Tvdb": "900"}}],
                   "Episode": [ep("a", 2, 1, tvdb="e1"), ep("a", 2, 2, tvdb="e-unbekannt")]}
    d = check(client)
    assert d["series"][0]["episodes"][0][:2] == [1, 13]
    assert d["problems"] == ["Anime: 1 Folgen ließen sich nicht zuordnen"]


def test_double_episode_and_unknown_series_and_movies(client):
    STATE["tv"][100] = show("S", seasons={1: ["2020-01-01"] * 3})
    STATE["movie"][7] = {"title": "Film", "original_title": "Film", "release_date": "2020-01-01", "status": "Released",
                         "overview": "x", "runtime": 90, "poster_path": None, "backdrop_path": None, "imdb_id": "tt1"}
    STATE["jf"] = {"Series": [{"Id": "a", "Name": "S", "ProviderIds": {"Tmdb": "100"}},
                              {"Id": "b", "Name": "Unbekannt", "ProviderIds": {}}],
                   "Episode": [ep("a", 1, 1, end=2), ep("b", 1, 1)],
                   "Movie": [{"Name": "Film", "ProviderIds": {"Tmdb": "7"}, "UserData": {"LastPlayedDate": "2024-01-01T10:00:00Z"}}]}
    d = check(client)
    assert d["episodes"] == 2 and len(d["movies"]) == 1 and "Unbekannt" in d["problems"][0]
    client.post("/konto/jellyfin/uebernehmen")
    assert logic.plain_items(1, "movie")[0]["state"] == "done"


def test_pagination(client):
    STATE["tv"][100] = show("S", seasons={1: ["2020-01-01"] * 620})
    STATE["jf"] = {"Series": [{"Id": "a", "Name": "S", "ProviderIds": {"Tmdb": "100"}}],
                   "Episode": [ep("a", 1, n) for n in range(1, 621)]}
    assert check(client)["episodes"] == 620


def test_clear_errors(client, monkeypatch):
    client.post("/konto/profil", data={"jellyfin_name": "Niemand"})
    d = check(client)
    assert d["state"] == "error" and "kein Konto" in d["error"]
    client.post("/konto/profil", data={"jellyfin_name": ""})
    monkeypatch.setenv("JELLYFIN_URL", "http://127.0.0.1:9")
    assert "nicht erreichbar" in check(client)["error"]
    assert "Erneut prüfen" in client.get("/konto").get_data(as_text=True)


def test_without_configuration(client, monkeypatch):
    monkeypatch.delenv("JELLYFIN_URL")
    assert "JELLYFIN_URL" in client.get("/konto").get_data(as_text=True)
    assert jh.status(1) is None


def test_preview_belongs_to_one_person(env, client):
    STATE["tv"][100] = show("S", seasons={1: ["2020-01-01"]})
    STATE["jf"] = {"Series": [{"Id": "a", "Name": "S", "ProviderIds": {"Tmdb": "100"}}], "Episode": [ep("a", 1, 1)]}
    check(client)
    zweiter = other(env, client)
    zweiter.post("/konto/jellyfin/uebernehmen")
    assert not watches()
