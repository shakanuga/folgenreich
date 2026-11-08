import io
import json

from PIL import Image, ImageDraw, ImageFont

from conftest import STATE, other
from folgenreich import db, logic

ZELDA = {"id": 1, "name": "The Legend of Zelda: Breath of the Wild", "first_release_date": 1488499200,
         "cover": {"image_id": "abc"}, "platforms": [{"name": "Nintendo Switch"}, {"name": "Wii U"}],
         "genres": [{"name": "Adventure"}, {"name": "Role-playing (RPG)"}], "summary": "Text"}


def add(client, platform="Nintendo Switch", fmt="Modul", action="sammlung"):
    page = client.get("/suche?art=spiel&q=zelda").get_data(as_text=True)
    assert "In die Sammlung" in page or "Hast du schon" in page
    payload = {"ext_id": "1", "title": ZELDA["name"], "year": 2017, "cover": "https://x/c.jpg",
               "genres": ["Adventure", "Role-playing (RPG)"], "overview": "Text"}
    return client.post("/hinzufuegen", data={"type": "game", "action": action, "payload": json.dumps(payload),
                                             "platform": platform, "format": fmt})


def mid():
    return db.get().execute("SELECT id FROM media WHERE type='game'").fetchone()[0]


def test_collection_flow_and_duplicate_hint(client):
    STATE["igdb"] = [ZELDA]
    add(client)
    assert logic.game_items(1)[0]["platforms"] == "Nintendo Switch"
    assert "Hast du schon: Nintendo Switch" in client.get("/suche?art=spiel&q=zelda").get_data(as_text=True)
    assert "Breath of the Wild" in client.get("/spiele").get_data(as_text=True)


def test_two_copies_on_two_platforms_are_one_game(client):
    STATE["igdb"] = [ZELDA]
    add(client)
    client.post(f"/m/{mid()}/exemplar", data={"platform": "Wii U", "format": "Disc", "condition": "Gut", "price": "29,99",
                                              "bought_on": "2019-05-01", "complete": "on", "location": "Regal"})
    s = logic.game_stats(1)
    assert s["games"] == 1 and s["copies"] == 2 and s["spent"] == 2999 and s["priced"] == 1
    assert {p["label"] for p in s["platforms"]} == {"Wii U", "Nintendo Switch"}


def test_edit_and_delete_copy(client):
    STATE["igdb"] = [ZELDA]
    add(client)
    c = logic.copies(1, mid())[0]
    client.post(f"/m/{mid()}/exemplar/{c['id']}", data={"platform": "Nintendo Switch", "price": "1.234,50", "condition": "Versiegelt"})
    c = logic.copies(1, mid())[0]
    assert c["price_cents"] == 123450 and c["condition"] == "Versiegelt"
    client.post(f"/m/{mid()}/exemplar/{c['id']}/entfernen")
    assert logic.copies(1, mid()) == []


def test_foreign_copy_is_protected(env, client):
    STATE["igdb"] = [ZELDA]
    add(client)
    c = logic.copies(1, mid())[0]
    zweiter = other(env, client)
    zweiter.post(f"/m/{mid()}/exemplar/{c['id']}/entfernen")
    zweiter.post(f"/m/{mid()}/exemplar/{c['id']}", data={"platform": "Gestohlen"})
    assert logic.copies(1, mid())[0]["platform"] == "Nintendo Switch"


def test_played_status_and_stats_page(client):
    STATE["igdb"] = [ZELDA]
    add(client)
    client.post(f"/m/{mid()}/status", data={"state": "done"})
    s = logic.game_stats(1)
    assert s["done_pct"] == 100 and s["unplayed"] == 0
    page = client.get("/spiele/statistik").get_data(as_text=True)
    assert "1 Spiel auf 1 Plattform" in page and "Adventure" in page and "2010er" in page
    assert "Breath of the Wild" in client.get("/spiele/durch").get_data(as_text=True)


def test_wishlist_moves_to_collection(client):
    STATE["igdb"] = [ZELDA]
    add(client, action="merken")
    assert logic.game_items(1)[0]["state"] == "planned"
    assert "auf der Wunschliste" in client.get("/suche?art=spiel&q=zelda").get_data(as_text=True)
    client.post(f"/m/{mid()}/exemplar", data={"platform": "Wii U"})
    assert logic.game_items(1)[0]["state"] == "backlog"


def test_manual_without_igdb(client, monkeypatch):
    monkeypatch.delenv("IGDB_ID")
    assert "fehlen IGDB_ID" in client.get("/suche?art=spiel&q=x").get_data(as_text=True)
    assert client.get("/spiel-foto").status_code == 200
    client.post("/spiel-von-hand", data={"title": "Altes Modul", "platform": "SNES", "genres": "Jump and Run, Retro", "year": "1993"})
    s = logic.game_stats(1)
    assert s["games"] == 1 and s["decades"][0]["label"] == "1990er" and s["genres"][0]["value"] == 1


def test_many_genres_fold_into_sonstige(client):
    for i in range(12):
        client.post("/spiel-von-hand", data={"title": f"Spiel {i}", "platform": "PC", "genres": f"Genre {i}"})
    s = logic.game_stats(1)
    assert len(s["genres"]) == 8 and s["genres"][-1]["label"] == "Sonstige" and s["genres"][-1]["value"] == 5


def test_photo_reads_cover_title(client):
    STATE["igdb"] = [ZELDA]
    im = Image.new("RGB", (1400, 500), "white")
    ImageDraw.Draw(im).text((60, 120), "Breath of the Wild", fill="black", font=ImageFont.load_default(size=100))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=90)
    r = client.post("/api/spiel-foto", data={"photo": (io.BytesIO(b.getvalue()), "c.jpg")}, content_type="multipart/form-data")
    assert "In die Sammlung" in r.get_data(as_text=True)


def test_movies_and_series_have_no_collection_features(client):
    html = client.get("/filme").get_data(as_text=True)
    assert "Regal" not in html and "Exemplar" not in html


def test_collection_shows_copy_details(client):
    STATE["igdb"] = [ZELDA]
    add(client)
    c = logic.copies(1, mid())[0]
    client.post(f"/m/{mid()}/exemplar/{c['id']}", data={"platform": "Nintendo Switch", "format": "Modul", "condition": "Gut",
                                                        "price": "45", "location": "Regal Flur", "edition": "Steelbook"})
    page = client.get("/spiele").get_data(as_text=True)
    assert "Nintendo Switch, Modul, Gut, Steelbook" in page and "Regal Flur" in page and "45,00 €" in page and "Adventure" in page


import pytest as _pt


@_pt.mark.parametrize("name,key", [("Wii", "wii"), ("Wii U", "wiiu"), ("PlayStation 3", "ps3"), ("PlayStation", "ps1"),
    ("Nintendo Switch 2", "switch2"), ("Nintendo Switch", "switch"), ("New Nintendo 2DS", "n3ds"), ("Nintendo DS", "nds"),
    ("Xbox Series X|S", "xsx"), ("Xbox 360", "x360"), ("PC (Microsoft Windows)", "pc"), ("Nintendo 64", "cart"),
    ("Sega Dreamcast", "other")])
def test_platform_case(name, key):
    assert logic.platform_case(name)["key"] == key


def test_shelf_shows_case(client):
    STATE["igdb"] = [ZELDA]
    add(client)
    client.post(f"/m/{mid()}/exemplar", data={"platform": "Wii U"})
    page = client.get("/spiele").get_data(as_text=True)
    assert "case case-switch" in page and "+1" in page


def test_platform_with_bar_stays_whole_in_filter(client):
    client.post("/spiel-von-hand", data={"title": "Forza", "platform": "Xbox Series X|S"})
    page = client.get("/spiele").get_data(as_text=True)
    assert 'data-sub="Xbox Series X|S"' in page and '>S</button>' not in page
