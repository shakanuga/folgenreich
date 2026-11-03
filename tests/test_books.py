import io
import json
import random

import pytest
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from conftest import STATE, other
from folgenreich import books, db, logic

ISBN = "9783446254800"
VOL = {"volumeInfo": {"title": "Der Name des Windes", "authors": ["Patrick Rothfuss"], "publishedDate": "2008-03-01",
                      "industryIdentifiers": [{"type": "ISBN_13", "identifier": ISBN}], "pageCount": 800}}


def ean_image(code, module=4, margin=40):
    bits = "101"
    par = books.PARITY[int(code[0])]
    for p, d in zip(par, code[1:7]):
        bits += (books.L if p == "L" else books.G)[int(d)]
    bits += "01010"
    for d in code[7:]:
        bits += books.R[int(d)]
    bits += "101"
    w = len(bits) * module + 2 * margin
    im = Image.new("L", (w, 300), 255)
    d = ImageDraw.Draw(im)
    for i, b in enumerate(bits):
        if b == "1":
            d.rectangle([margin + i * module, 30, margin + (i + 1) * module - 1, 260], fill=0)
    return im


def in_scene(barcode, size=(1500, 2000)):
    """Strichcode auf einer unruhigen Buchrückseite"""
    scene = Image.new("L", size, 190)
    d = ImageDraw.Draw(scene)
    random.seed(3)
    for _ in range(40):
        y = random.randrange(size[1])
        d.text((random.randrange(size[0]), y), "Ein spannender Roman ueber Wind", fill=60)
    scene.paste(barcode, (500, 1400))
    return scene.convert("RGB")


def jpeg(im, q=75):
    b = io.BytesIO()
    im.convert("RGB").save(b, "JPEG", quality=q)
    return b.getvalue()


def test_isbn_cleaning():
    assert books.clean_isbn("978-3-446-25480-0") == ISBN
    assert books.clean_isbn("3446254803") and books.clean_isbn("3446254803").startswith("978")
    assert books.clean_isbn("978-3-446-25480-1") is None
    assert books.clean_isbn("4006381333931") is None         # normale Ware, kein Buch


@pytest.mark.parametrize("module", [2, 3, 4, 7])
def test_barcode_sizes(module):
    assert books.read_barcode(ean_image(ISBN, module)) == ISBN


@pytest.mark.parametrize("code", ["9783446254800", "9780306406157", "9791034900008"])
def test_barcode_codes(code):
    assert books.read_barcode(ean_image(code, 3)) == code


@pytest.mark.parametrize("angle", [0, 5, 90, 180, 270, -12])
def test_barcode_in_photo_like_scene(angle):
    img = in_scene(ean_image(ISBN, 4)).rotate(angle, expand=True, fillcolor=(190, 190, 190))
    img = img.filter(ImageFilter.GaussianBlur(1.2))
    assert books.read_barcode(books.load_photo(jpeg(img))) == ISBN


def test_uneven_light():
    im = in_scene(ean_image(ISBN, 4)).convert("L")
    grad = Image.linear_gradient("L").resize(im.size).rotate(90)
    dim = Image.blend(im, grad, 0.35)
    assert books.read_barcode(dim.convert("RGB")) == ISBN


def test_no_false_barcode_in_text_noise():
    random.seed(1)
    im = Image.effect_noise((900, 900), 90).convert("RGB")
    assert books.read_barcode(im) is None


def test_ocr_finds_isbn_and_title():
    im = Image.new("RGB", (1400, 500), "white")
    d = ImageDraw.Draw(im)
    f = ImageFont.load_default(size=56)
    d.text((60, 60), "ISBN 978-3-446-25480-0", fill="black", font=f)
    assert books.isbn_in_text(books.ocr(im)) == ISBN
    im = Image.new("RGB", (1400, 600), "white")
    d = ImageDraw.Draw(im)
    d.text((60, 60), "Der Name des Windes", fill="black", font=ImageFont.load_default(size=90))
    d.text((60, 300), "Patrick Rothfuss", fill="black", font=f)
    r = books.analyse_photo(jpeg(im, 90))
    assert r["how"] == "cover" and "Name des Windes" in r["query"]


def photo(client):
    return client.post("/api/buch-foto", data={"photo": (io.BytesIO(jpeg(in_scene(ean_image(ISBN)))), "b.jpg")},
                       content_type="multipart/form-data").get_data(as_text=True)


def test_photo_flow_then_duplicate_warning(client):
    STATE["google"][f"isbn:{ISBN}"] = [VOL]
    page = photo(client)
    assert "Der Name des Windes" in page and "Ins Regal" in page
    b = books.by_isbn(ISBN)
    client.post("/hinzufuegen", data={"type": "book", "action": "regal", "payload": json.dumps(b)})
    assert "Hast du schon, noch ungelesen" in photo(client)
    m = logic.plain_items(1, "book")[0]
    assert m["owned"] == 1 and m["pages"] == 800
    client.post(f"/m/{m['id']}/status", data={"state": "done"})
    assert "Hast du schon, gelesen" in photo(client)
    page = client.get("/buecher").get_data(as_text=True)
    assert "1 Buch im Regal, davon 0 ungelesen" in page


def test_german_book_only_in_dnb(client):
    STATE["dnb"] = f"""<searchRetrieveResponse xmlns="http://www.loc.gov/zing/srw/"><records><record><recordData>
      <dc xmlns="http://www.openarchives.org/OAI/2.0/oai_dc/" xmlns:dc="http://purl.org/dc/elements/1.1/">
      <dc:title>Dorfgeschichten : Roman / Anna Muster</dc:title><dc:creator>Muster, Anna [Verfasser]</dc:creator>
      <dc:publisher>Kleinverlag</dc:publisher><dc:date>2019</dc:date><dc:identifier>ISBN {ISBN}</dc:identifier>
      </dc></recordData></record></records></searchRetrieveResponse>"""
    b = books.by_isbn(ISBN)
    assert b["title"] == "Dorfgeschichten: Roman" and b["authors"] == "Muster, Anna" and b["year"] == 2019


def test_manual_book_with_cover(client):
    client.post("/buch-von-hand", data={"title": "Familienchronik", "authors": "Autorin", "action": "regal",
                                        "photo": (io.BytesIO(jpeg(Image.new("RGB", (800, 1200), "teal"))), "c.jpg")},
                content_type="multipart/form-data")
    m = logic.plain_items(1, "book")[0]
    assert m["title"] == "Familienchronik" and m["poster"].startswith("/cover/")
    assert client.get(m["poster"]).status_code == 200


def test_lists_are_per_person(env, client):
    STATE["google"][f"isbn:{ISBN}"] = [VOL]
    b = books.by_isbn(ISBN)
    client.post("/hinzufuegen", data={"type": "book", "action": "gelesen", "payload": json.dumps(b)})
    zweiter = other(env, client)
    assert "Ins Regal" in photo(zweiter)
    zweiter.post("/hinzufuegen", data={"type": "book", "action": "regal", "payload": json.dumps(b)})
    assert logic.plain_items(2, "book")[0]["state"] == "planned"
    assert logic.plain_items(1, "book")[0]["state"] == "done"
    assert db.get().execute("SELECT COUNT(*) FROM media WHERE type='book'").fetchone()[0] == 1


def test_unreadable_upload(client):
    r = client.post("/api/buch-foto", data={"photo": (io.BytesIO(b"kein bild"), "x.jpg")}, content_type="multipart/form-data")
    assert r.status_code == 400
