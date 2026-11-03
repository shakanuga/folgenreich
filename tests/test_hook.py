from conftest import STATE, other, show
from folgenreich import db, logic

BASE = {"NotificationType": "PlaybackStop", "PlayedToCompletion": True, "NotificationUsername": "nutzer1", "ItemType": "Episode",
        "SeriesName": "Die Serie", "SeasonNumber": 1, "EpisodeNumber": 2, "Provider_tvdb": "555"}


def url():
    return "/hook/jellyfin/" + db.setting("hook_token")


def setup(client):
    client.get("/konto")
    STATE["tv"][77] = show("Die Serie", seasons={1: ["2020-01-01"] * 3})


def watched():
    return db.get().execute("SELECT season,number FROM watches ORDER BY 1,2").fetchall()


def test_tvdb_episode_id_resolves_and_adds_unknown_show(client):
    setup(client)
    # TheTVDB zählt anders als TMDB: Staffel 1 Folge 2 ist bei TMDB Folge 3
    STATE["find"]["555"] = {"tv_episode_results": [{"show_id": 77, "season_number": 1, "episode_number": 3}]}
    assert client.post(url(), json=BASE).json["result"] == "ok"
    assert [tuple(r) for r in watched()] == [(1, 3)]
    assert logic.tv_items(1)[0]["title"] == "Die Serie"


def test_name_fallback_only_for_tracked_shows(client):
    setup(client)
    p = dict(BASE, Provider_tvdb=None)
    assert client.post(url(), json=p).json["result"] == "unmatched"
    client.post("/hinzufuegen", data={"type": "tv", "ext_id": 77, "action": "schaue"})
    assert client.post(url(), json=p).json["result"] == "ok"
    assert "abgehakt" in client.get("/konto").get_data(as_text=True)


def test_ignored_cases(client):
    setup(client)
    STATE["find"]["555"] = {"tv_episode_results": [{"show_id": 77, "season_number": 1, "episode_number": 2}]}
    assert client.post(url(), json=dict(BASE, PlayedToCompletion=False)).json["result"] == "ignored"
    assert client.post(url(), json=dict(BASE, NotificationType="PlaybackStart")).json["result"] == "ignored"
    assert client.post(url(), json=dict(BASE, NotificationUsername="Gast")).json["result"] == "ignored"
    assert not watched()
    assert client.post("/hook/jellyfin/falsch", json=BASE).status_code == 404


def test_jellyfin_name_maps_to_account(env, client):
    setup(client)
    STATE["find"]["555"] = {"tv_episode_results": [{"show_id": 77, "season_number": 1, "episode_number": 2}]}
    zweiter = other(env, client)
    zweiter.post("/konto/profil", data={"jellyfin_name": "JellyName"})
    assert client.post(url(), json=dict(BASE, NotificationUsername="jellyname")).json["result"] == "ok"
    assert db.get().execute("SELECT user_id FROM watches").fetchone()[0] == 2


def test_string_flags_and_movie(client):
    setup(client)
    STATE["movie"][42] = {"title": "Film", "original_title": "Film", "release_date": "2020-01-01", "status": "Released",
                          "overview": "x", "runtime": 100, "poster_path": None, "backdrop_path": None, "imdb_id": "tt1"}
    p = {"NotificationType": "PlaybackStop", "PlayedToCompletion": "True", "NotificationUsername": "nutzer1", "ItemType": "Movie",
         "Name": "Film", "Provider_tmdb": "42"}
    assert client.post(url(), json=p).json["result"] == "ok"
    assert logic.plain_items(1, "movie")[0]["state"] == "done"
