import io
import json

import pytest
from PIL import Image, ImageDraw, ImageFont

from conftest import STATE, other
from folgenreich import books, db, games, logic
from test_books import ean_image, in_scene, jpeg

EAN = "5030917285394"   # kein Buch, normaler Spiele-Code
ELDEN = {"id": 10, "name": "Elden Ring", "first_release_date": 1645747200, "cover": {"image_id": "a"},
         "platforms": [{"name": "PlayStation 5"}, {"name": "PC (Microsoft Windows)"}], "genres": [{"name": "RPG"}]}
OTHER = {"id": 11, "name": "Ring Fit Adventure", "platforms": [{"name": "Nintendo Switch"}], "genres": []}


def snap(client, image):
    return client.post("/api/spiel-foto", data={"photo": (io.BytesIO(jpeg(image, 90)), "b.jpg")},
                       content_type="multipart/form-data").get_data(as_text=True)


def cover():
    im = Image.new("RGB", (900, 1200), (20, 24, 40))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 900, 90], fill=(0, 70, 180))
    d.text((30, 20), "PS5", fill="white", font=ImageFont.load_default(size=50))
    d.text((120, 300), "ELDEN RING", fill=(235, 215, 150), font=ImageFont.load_default(size=100))
    d.text((40, 1100), "USK ab 16", fill="white", font=ImageFont.load_default(size=40))
    return im


def test_any_ean_is_read_but_books_stay_books():
    im = ean_image(EAN, 3)
    assert books.read_code(im) == EAN
    assert books.read_barcode(im) is None


def test_barcode_found_in_product_database(client):
    STATE["upc"][EAN] = "Elden Ring - PlayStation 5 (Standard Edition)"
    STATE["igdb"] = [ELDEN]
    page = snap(client, in_scene(ean_image(EAN, 4)))
    assert "Über den Strichcode gefunden" in page and "Elden Ring" in page
    assert STATE["igdb_queries"] == ["Elden Ring"]
    assert '<option selected>PlayStation 5</option>' in page


def test_unknown_barcode_is_learned_for_everyone(env, client):
    STATE["igdb"] = [ELDEN]
    page = snap(client, in_scene(ean_image(EAN, 4)))
    assert f"Strichcode {EAN} erkannt" in page and 'name="ean"' in page
    payload = {"ext_id": "10", "title": "Elden Ring", "genres": []}
    client.post("/hinzufuegen", data={"type": "game", "action": "sammlung", "payload": json.dumps(payload),
                                      "platform": "PlayStation 5", "format": "Disc", "ean": EAN})
    again = snap(client, in_scene(ean_image(EAN, 4)))
    assert "Dieser Strichcode gehört zu" in again and "Elden Ring" in again
    assert "Dieser Strichcode gehört zu" in other(env, client).post("/api/spiel-foto", data={"ean": EAN}).get_data(as_text=True)


def test_barcode_can_be_linked_to_owned_game(client):
    payload = {"ext_id": "10", "title": "Elden Ring", "genres": []}
    client.post("/hinzufuegen", data={"type": "game", "action": "sammlung", "payload": json.dumps(payload), "platform": "PC"})
    STATE["igdb"] = [ELDEN]
    assert "Strichcode diesem Spiel zuordnen" in client.get(f"/suche?art=spiel&q=elden&ean={EAN}").get_data(as_text=True)
    client.post("/hinzufuegen", data={"type": "game", "action": "zuordnen", "payload": json.dumps(payload), "ean": EAN})
    assert games.known_ean(EAN)["title"] == "Elden Ring"
    assert len(logic.copies(1, games.known_ean(EAN)["media_id"])) == 1


def test_cover_ocr_ranks_right_game_and_detects_platform(client):
    STATE["igdb"] = [OTHER, ELDEN]
    page = snap(client, cover())
    assert page.index("Elden Ring") < page.index("Ring Fit") if "Ring Fit" in page else "Elden Ring" in page
    assert '<option selected>PlayStation 5</option>' in page


def test_correction_box_keeps_code_and_text(client):
    page = snap(client, Image.new("RGB", (500, 700), "white"))
    assert 'action="/suche"' in page and "kein Titel lesbar" in page


@pytest.mark.parametrize("raw,clean", [("Elden Ring (PS5)", "Elden Ring"), ("Mario Kart 8 Deluxe - Nintendo Switch", "Mario Kart 8"),
                                       ("[PlayStation 4] God of War", "God of War"), ("FIFA 23 für PS4", "FIFA 23")])
def test_clean_title(raw, clean):
    assert games.clean_title(raw) == clean


def test_platform_words():
    assert games.detect_platform("Elden Ring PS5") == "PlayStation 5"
    assert games.detect_platform("Nintendo Switch 2 Edition") == "Nintendo Switch 2"
    assert games.detect_platform("Nix") is None


def test_noise_lines_are_not_searched():
    assert games.candidates(["PEGI 16", "Nintendo Switch", "x", "The Witcher 3 Wild Hunt"]) == ["The Witcher 3 Wild Hunt"]
