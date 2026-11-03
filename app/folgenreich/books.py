"""Bücher finden: per Text, per ISBN, per Foto.

Quellen: Google Books, Open Library und die Deutsche Nationalbibliothek. Keine
davon braucht zwingend einen Schlüssel. Die DNB fängt deutsche Bücher auf, die
in den anderen beiden fehlen.
"""
import io
import os
import re
import subprocess
import xml.etree.ElementTree as ET

import requests
from PIL import Image, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

TIMEOUT = 12
UA = {"User-Agent": "Folgenreich/1.0 (selbst gehosteter Medientracker)"}


def base(name, default):
    return os.environ.get(name, default)


def clean_isbn(text):
    s = re.sub(r"[^0-9Xx]", "", text or "").upper()
    if len(s) == 10 and _valid10(s):
        core = "978" + s[:9]
        return core + _check13(core)
    if len(s) == 13 and s[:3] in ("978", "979") and s[-1] == _check13(s[:12]):
        return s
    return None


def _valid10(s):
    return sum((10 if c == "X" else int(c)) * (10 - i) for i, c in enumerate(s)) % 11 == 0 and "X" not in s[:9]


def _check13(core):
    return str((10 - sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(core)) % 10) % 10)


def _book(title, authors=None, year=None, isbn=None, cover=None, publisher=None, pages=None, overview=None):
    return {"title": title, "authors": ", ".join(a for a in (authors or []) if a) or None,
            "year": year, "isbn": isbn, "cover": cover, "publisher": publisher, "pages": pages, "overview": overview}


def _json(url, params):
    try:
        r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
        return r.json() if r.ok else {}
    except (requests.RequestException, ValueError):
        return {}


def google(query):
    params = {"q": query, "maxResults": 12, "printType": "books", "langRestrict": ""}
    if os.environ.get("GOOGLE_BOOKS_API_KEY"):
        params["key"] = os.environ["GOOGLE_BOOKS_API_KEY"]
    data = _json(base("GOOGLE_BOOKS_BASE", "https://www.googleapis.com/books/v1") + "/volumes", params)
    out = []
    for item in data.get("items", []):
        v = item.get("volumeInfo", {})
        isbn = None
        for ident in v.get("industryIdentifiers", []):
            isbn = clean_isbn(ident.get("identifier")) or isbn
        cover = (v.get("imageLinks") or {}).get("thumbnail")
        title = v.get("title", "")
        if v.get("subtitle"):
            title += ": " + v["subtitle"]
        if title:
            out.append(_book(title, v.get("authors"), _year(v.get("publishedDate")), isbn,
                             cover.replace("http://", "https://") if cover else None, v.get("publisher"),
                             v.get("pageCount"), v.get("description")))
    return out


def openlibrary(query=None, isbn=None):
    params = {"limit": 12, "fields": "title,author_name,first_publish_year,isbn,cover_i,publisher,number_of_pages_median"}
    params.update({"isbn": isbn} if isbn else {"q": query})
    data = _json(base("OPENLIBRARY_BASE", "https://openlibrary.org") + "/search.json", params)
    out = []
    for d in data.get("docs", []):
        found = isbn or next((c for c in map(clean_isbn, d.get("isbn") or []) if c), None)
        cover = f"https://covers.openlibrary.org/b/id/{d['cover_i']}-M.jpg" if d.get("cover_i") else None
        out.append(_book(d.get("title"), d.get("author_name"), d.get("first_publish_year"), found, cover,
                         (d.get("publisher") or [None])[0], d.get("number_of_pages_median")))
    return [b for b in out if b["title"]]


def dnb(query=None, isbn=None):
    cql = f"num={isbn}" if isbn else 'tit="' + query.replace('"', " ") + '"'
    params = {"version": "1.1", "operation": "searchRetrieve", "query": cql, "recordSchema": "oai_dc", "maximumRecords": 10}
    try:
        r = requests.get(base("DNB_BASE", "https://services.dnb.de/sru/dnb"), params=params, headers=UA, timeout=TIMEOUT)
        root = ET.fromstring(r.content)
    except (requests.RequestException, ET.ParseError):
        return []
    ns = "{http://purl.org/dc/elements/1.1/}"
    out = []
    for rec in root.iter():
        if not rec.tag.endswith("}dc") and rec.tag != "dc":
            continue
        title = rec.findtext(ns + "title") or ""
        title = re.sub(r"\s*/\s.*$", "", title).replace(" : ", ": ").strip()
        ids = [clean_isbn(e.text) for e in rec.findall(ns + "identifier") if "ISBN" in (e.text or "")
               or "isbn" in str(e.attrib).lower()]
        found = isbn or next((i for i in ids if i), None)
        people = [re.sub(r"\s*\[.*?\]", "", e.text or "") for e in rec.findall(ns + "creator")]
        if title:
            out.append(_book(title, people, _year(rec.findtext(ns + "date")), found,
                             f"https://portal.dnb.de/opac/mvb/cover?isbn={found}" if found else None,
                             rec.findtext(ns + "publisher")))
    return out


def _year(text):
    m = re.search(r"\d{4}", text or "")
    return int(m.group()) if m else None


def _is_image(url):
    """Lädt die Adresse an: Nicht jede Quelle hat zu jeder ISBN wirklich ein Bild."""
    try:
        with requests.get(url, headers=UA, timeout=6, stream=True) as r:
            if not r.ok or not r.headers.get("Content-Type", "").startswith("image/"):
                return False
            data = next(r.iter_content(4096), b"")
            return len(data) > 1000  # Quellen liefern als Platzhalter oft ein winziges Bild
    except requests.RequestException:
        return False


def best_cover(isbn, current=None):
    """Erste Adresse, die wirklich ein Bild liefert."""
    options = [current]
    if isbn:
        options += [base("OPENLIBRARY_COVERS", "https://covers.openlibrary.org") + f"/b/isbn/{isbn}-L.jpg?default=false",
                    base("DNB_COVERS", "https://portal.dnb.de/opac/mvb/cover") + f"?isbn={isbn}"]
    return next((u for u in options if u and _is_image(u)), None)


def by_isbn(isbn):
    """Alle Quellen fragen und das vollständigste Ergebnis zusammensetzen."""
    hits = google(f"isbn:{isbn}")[:1] + openlibrary(isbn=isbn)[:1] + dnb(isbn=isbn)[:1]
    if not hits:
        return None
    merged = dict(hits[0])
    for h in hits[1:]:
        for k, v in h.items():
            merged[k] = merged.get(k) or v
    merged["isbn"] = isbn
    merged["cover"] = best_cover(isbn, merged.get("cover"))
    return merged


def search(query):
    isbn = clean_isbn(query)
    if isbn:
        hit = by_isbn(isbn)
        return [hit] if hit else []
    results = google(query) + openlibrary(query)
    if len(results) < 4:
        results += dnb(query)
    seen, out = set(), []
    for b in results:
        key = b["isbn"] or re.sub(r"\W", "", b["title"].lower())
        if key not in seen:
            seen.add(key)
            out.append(b)
    return sorted(out, key=lambda b: not b["cover"])[:15]


# --- Foto auswerten ------------------------------------------------------------------------------

L = ["0001101", "0011001", "0010011", "0111101", "0100011", "0110001", "0101111", "0111011", "0110111", "0001011"]
G = ["".join(reversed("".join("1" if c == "0" else "0" for c in code))) for code in L]
R = ["".join("1" if c == "0" else "0" for c in code) for code in L]
PARITY = ["LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG", "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL"]


def _widths(code):
    out, run = [], 1
    for a, b in zip(code, code[1:]):
        if a == b:
            run += 1
        else:
            out.append(run)
            run = 1
    return out + [run]


_LEFT = {"L": [_widths(c) for c in L], "G": [_widths(c) for c in G]}
_RIGHT = [_widths(c) for c in R]


def _digit(runs, table):
    total = sum(runs)
    if not total:
        return None, 9
    scaled = [r * 7 / total for r in runs]
    best, score = None, 9.0
    for d, pattern in enumerate(table):
        err = sum(abs(a - b) for a, b in zip(scaled, pattern))
        if err < score:
            best, score = d, err
    return best, score


def _decode_runs(runs, quiet_before, quiet_after):
    """runs beginnen mit einem schwarzen Balken des Startmusters. Die Prüfungen
    auf Ruhezone und gleichmäßige Breiten verhindern Treffer in Text oder Rauschen."""
    module = sum(runs) / 95
    if not module or max(runs[:3]) > 2 * min(runs[:3]) + 1:
        return None
    if quiet_before < 4 * module or quiet_after < 4 * module:
        return None
    guards = runs[:3] + runs[27:32] + runs[56:59]
    if max(guards) > 2.2 * module or min(guards) < 0.35 * module:
        return None
    cells = [sum(runs[i:i + 4]) for i in list(range(3, 27, 4)) + list(range(32, 56, 4))]
    if any(abs(c - 7 * module) > 1.75 * module for c in cells):
        return None
    digits, parity, i = [], "", 3
    for _ in range(6):
        cell = runs[i:i + 4]
        dl, el = _digit(cell, _LEFT["L"])
        dg, eg = _digit(cell, _LEFT["G"])
        if min(el, eg) > 2.2:
            return None
        if el <= eg:
            digits.append(dl)
            parity += "L"
        else:
            digits.append(dg)
            parity += "G"
        i += 4
    i += 5
    for _ in range(6):
        d, e = _digit(runs[i:i + 4], _RIGHT)
        if e > 2.2:
            return None
        digits.append(d)
        i += 4
    if parity not in PARITY:
        return None
    code = str(PARITY.index(parity)) + "".join(map(str, digits))
    return clean_isbn(code)


def _rows(img):
    w, h = img.size
    px = img.tobytes()
    for frac in range(1, 90):
        y = h * frac // 90
        yield px[y * w:(y + 1) * w]


def _scan(img):
    w = img.size[0]
    hits = {}
    for line in _rows(img):
        lo, hi = min(line), max(line)
        if hi - lo < 50:
            continue
        window = max(40, w // 8)
        # gleitender Mittelwert als Schwelle: hält ungleichmäßiges Licht aus
        acc = [0]
        for v in line:
            acc.append(acc[-1] + v)
        runs, dark, cur = [], None, 0
        for x, v in enumerate(line):
            a, b = max(0, x - window), min(w, x + window)
            d = v < (acc[b] - acc[a]) / (b - a) * 0.95
            if d == dark:
                cur += 1
            else:
                if dark is not None:
                    runs.append((dark, cur))
                dark, cur = d, 1
        runs.append((dark, cur))
        widths = [r[1] for r in runs]
        for i, (is_dark, _) in enumerate(runs):
            if is_dark and i + 59 <= len(runs):
                before = widths[i - 1] if i else widths[0] * 9
                after = widths[i + 59] if i + 59 < len(runs) else widths[-1] * 9
                hit = _decode_runs(widths[i:i + 59], before, after)
                if hit:
                    hits[hit] = hits.get(hit, 0) + 1
                    if hits[hit] >= 2:  # zwei Zeilen müssen übereinstimmen
                        return hit
    return None


def read_barcode(image):
    """Sucht einen ISBN-Strichcode (EAN-13). Probiert Größen und Drehungen durch."""
    gray = ImageOps.autocontrast(image.convert("L"))
    for width in (1600, 1000, 2400):
        scaled = gray.copy()
        scaled.thumbnail((width, width))
        for angle in (0, 90, 180, 270, 8, -8, 15, -15):
            rotated = scaled if angle == 0 else scaled.rotate(angle, expand=True, fillcolor=255)
            hit = _scan(rotated)
            if hit:
                return hit
    return None


def ocr(image):
    buf = io.BytesIO()
    image.convert("L").save(buf, "PNG")
    try:
        p = subprocess.run(["tesseract", "stdin", "stdout", "-l", "deu+eng", "--psm", "3"], input=buf.getvalue(),
                           capture_output=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout.decode("utf-8", "ignore")


def isbn_in_text(text):
    for m in re.finditer(r"(97[89][\d\s\-–]{10,16}\d|\b\d[\d\-–\s]{8,11}[\dXx]\b)", text):
        isbn = clean_isbn(m.group())
        if isbn:
            return isbn
    return None


def query_from_text(text):
    """Macht aus erkanntem Covertext einen Suchbegriff: die größten sinnvollen Zeilen."""
    lines = [re.sub(r"[^\wäöüÄÖÜß' \-]", " ", l).strip() for l in text.splitlines()]
    lines = [re.sub(r"\s+", " ", l) for l in lines if sum(c.isalpha() for c in l) >= 3]
    return " ".join(lines[:4])[:90]


def load_photo(data):
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((2600, 2600))
    return img


def analyse_photo(data):
    img = load_photo(data)
    isbn = read_barcode(img)
    if isbn:
        return {"isbn": isbn, "how": "barcode"}
    text = ocr(img)
    isbn = isbn_in_text(text)
    if isbn:
        return {"isbn": isbn, "how": "text"}
    return {"isbn": None, "how": "cover", "query": query_from_text(text)}
