import io

from conftest import STATE, other, show
from folgenreich import db, logic, sync

TV = 100


def add(client, action="schaue", ext=TV):
    return client.post("/hinzufuegen", data={"type": "tv", "ext_id": ext, "action": action, "title": "X"})


def mid(ext=TV):
    return db.get().execute("SELECT id FROM media WHERE ext_id=?", (str(ext),)).fetchone()[0]


def tab(user=1, ext=TV):
    return logic.tv_items(user, mid(ext))[0]["tab"]


def resync(ext=TV):
    sync.sync_media(mid(ext))


def api(client, ext, what, **body):
    return client.post(f"/api/m/{mid(ext)}/{what}", json=body)


def test_new_show_flow_through_all_lists(client):
    STATE["tv"][TV] = show("Die Serie", seasons={1: ["2026-01-01", "2026-01-08"]}, tvdb=9)
    add(client)
    assert tab() == "aktiv"
    assert api(client, TV, "next").json["label"] == "S01E01"
    r = api(client, TV, "next").json
    assert r["tab"] == "warten"                               # aktuell, Serie läuft weiter
    # neue Folge erscheint -> automatisch zurück nach Aktiv
    STATE["tv"][TV]["seasons_data"][1].append("2026-05-30")
    resync()
    assert tab() == "aktiv"
    api(client, TV, "next")
    # Serie endet -> Archiv
    STATE["tv"][TV]["status"] = "Ended"
    resync()
    assert tab() == "archiv"
    # neue Staffel angekündigt -> aus dem Archiv heraus, ohne Termin: Warten
    STATE["tv"][TV]["status"] = "Returning Series"
    STATE["tv"][TV]["seasons_data"][2] = [None, None]
    resync()
    assert tab() == "warten"
    # Termin in der Zukunft: weiter Warten, mit Datum
    STATE["tv"][TV]["seasons_data"][2] = ["2026-09-01", "2026-09-08"]
    resync()
    item = logic.tv_items(1, mid())[0]
    assert item["tab"] == "warten" and item["next_air"] == "2026-09-01"
    # läuft an -> Aktiv
    STATE["tv"][TV]["seasons_data"][2][0] = "2026-05-31"
    resync()
    assert tab() == "aktiv"


def test_new_season_continues_as_one_show(client):
    STATE["tv"][TV] = show("S", seasons={1: ["2020-01-01"], 2: ["2026-05-01", "2026-05-02"]})
    add(client)
    api(client, TV, "next")
    item = logic.tv_items(1, mid())[0]
    assert logic.pos_label(item["next_pos"]) == "S02E01" and item["open_eps"] == 2
    assert db.get().execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1


def test_mark_until_and_season(client):
    STATE["tv"][TV] = show("S", seasons={1: ["2020-01-01"] * 3, 2: ["2020-02-01"] * 3, 3: ["2030-01-01"]})
    add(client)
    api(client, TV, "until", pos=200002)
    item = logic.tv_items(1, mid())[0]
    assert item["watched"] == 5 and item["open_eps"] == 1
    api(client, TV, "season", season=2, on=False)
    assert logic.tv_items(1, mid())[0]["watched"] == 3


def test_gaps_and_specials_do_not_hold_show_open(client):
    STATE["tv"][TV] = show("S", "Ended", seasons={0: ["2020-01-01"] * 2, 1: ["2020-01-01"] * 2, 2: ["2020-02-01"] * 2})
    add(client)
    api(client, TV, "season", season=2)
    assert tab() == "archiv"                                 # Staffel 1 nie gesehen, Specials ohnehin nicht


def test_future_episodes_are_not_marked(client):
    STATE["tv"][TV] = show("S", seasons={1: ["2020-01-01", "2030-01-01"]})
    add(client, "gesehen")
    item = logic.tv_items(1, mid())[0]
    assert item["watched"] == 1 and item["tab"] == "warten"


def test_manual_archive_returns_on_new_episode(client):
    STATE["tv"][TV] = show("S", seasons={1: ["2026-01-01"]})
    add(client, "gesehen")
    m = mid()
    client.post(f"/m/{m}/status", data={"state": "archived"})
    assert tab() == "archiv"
    STATE["tv"][TV]["seasons_data"][1].append("2099-01-01")
    resync()
    assert tab() == "archiv"
    STATE["tv"][TV]["seasons_data"][1][1] = "2026-06-01"
    db.get().execute("UPDATE entries SET archived_at='2026-05-01 10:00:00'")
    db.get().commit()
    resync()
    assert tab() == "aktiv"


def test_dropped_stays_dropped_until_watched(client):
    STATE["tv"][TV] = show("S", seasons={1: ["2026-01-01", "2026-01-02"]})
    add(client)
    client.post(f"/m/{mid()}/status", data={"state": "dropped"})
    STATE["tv"][TV]["seasons_data"][1].append("2026-05-30")
    resync()
    assert logic.tv_items(1, mid())[0]["sub"] == "dropped"
    api(client, TV, "next")
    assert tab() == "aktiv"


def test_planned_show_on_merkliste_gets_start_date(client):
    STATE["tv"][TV] = show("Geplant", "Planned")
    add(client, "merken")
    assert tab() == "merkliste"
    assert "Noch ohne Termin" in client.get("/serien/merkliste").get_data(as_text=True)
    STATE["tv"][TV]["seasons_data"][1] = ["2026-12-01"]
    resync()
    assert "01.12.2026" in client.get("/serien/merkliste").get_data(as_text=True)


def test_anime_is_sorted_apart_and_can_be_changed(client):
    STATE["tv"][TV] = show("Frieren", genres=[16], country=["JP"], seasons={1: ["2026-01-01"]})
    add(client)
    assert "Frieren" in client.get("/anime").get_data(as_text=True)
    assert "Frieren" not in client.get("/serien").get_data(as_text=True)
    client.post(f"/m/{mid()}/art")
    resync()                                                 # Abgleich überschreibt die Hand-Korrektur nicht
    assert "Frieren" in client.get("/serien").get_data(as_text=True)


def test_users_are_separate(env, client):
    STATE["tv"][TV] = show("S", seasons={1: ["2026-01-01"] * 2})
    add(client)
    api(client, TV, "next")
    zweiter = other(env, client)
    assert "S" not in [i["title"] for i in logic.tv_items(2)]
    zweiter.post("/hinzufuegen", data={"type": "tv", "ext_id": TV, "action": "schaue"})
    assert logic.tv_items(2, mid())[0]["watched"] == 0
    assert logic.tv_items(1, mid())[0]["watched"] == 1
    assert zweiter.get("/konto").status_code == 200
    assert zweiter.post("/konto/nutzer", data={"username": "x"}).status_code == 403


def test_login_required_and_foreign_origin(env, client):
    assert env.test_client().get("/serien").status_code == 302
    assert client.post("/konto/profil", headers={"Origin": "https://boese.example"}).status_code == 403
    bad = env.test_client()
    assert "stimmt nicht" in bad.post("/anmelden", data={"username": "nutzer1", "password": "falsch"}).get_data(as_text=True)


def test_every_page_renders(client):
    STATE["tv"][TV] = show("S", seasons={1: ["2026-01-01"] * 2})
    add(client)
    for url in ("/", "/serien", "/serien/warten", "/serien/archiv", "/serien/merkliste", "/anime", "/filme",
                "/filme/gesehen", "/buecher", "/buecher/gelesen", "/suche", "/buch-foto", "/konto", f"/m/{mid()}",
                "/konto/export.json", "/manifest.webmanifest"):
        assert client.get(url).status_code == 200, url


def test_tmdb_outage_keeps_app_usable(client, monkeypatch):
    STATE["tv"][TV] = show("S", seasons={1: ["2026-01-01"]})
    add(client)
    monkeypatch.setenv("TMDB_BASE", "http://127.0.0.1:9/3")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    db.get().execute("UPDATE media SET synced_at=NULL")
    db.get().commit()
    assert sync.run_once() == 0
    assert client.get("/serien").status_code == 200
    assert "nicht erreichbar" in db.get().execute("SELECT sync_error FROM media").fetchone()[0]
