"""Spiele erkennen: zuerst über den Strichcode, sonst über den Text auf dem Cover.

Einen frei nutzbaren, vollständigen Strichcode-Katalog für Spiele gibt es nicht.
Deshalb merkt sich die App jede Zuordnung: Wird ein unbekannter Code einmal einem
Spiel zugewiesen, klappt er ab dann sofort, für alle Konten.

Cover-Schriftzüge sind stark gestaltet. Darum liest die Texterkennung das Bild in
mehreren Varianten, sucht mit den besten Zeilen und sortiert die Treffer danach,
wie gut sie zum erkannten Text passen.
"""
import difflib
import io
import logging
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import ImageOps

from . import db, igdb

log = logging.getLogger("gunicorn.error")

PLATFORM_WORDS = [("PlayStation 5", r"\b(ps ?5|playstation ?5)\b"), ("PlayStation 4", r"\b(ps ?4|playstation ?4)\b"),
                  ("PlayStation 3", r"\b(ps ?3|playstation ?3)\b"), ("PlayStation 2", r"\b(ps ?2|playstation ?2)\b"),
                  ("Xbox Series X|S", r"xbox series"), ("Xbox One", r"xbox ?one"), ("Xbox 360", r"xbox ?360"),
                  ("Nintendo Switch 2", r"switch ?2"), ("Nintendo Switch", r"\bswitch\b"), ("Wii U", r"\bwii ?u\b"),
                  ("Wii", r"\bwii\b"), ("Nintendo 3DS", r"\b3ds\b"), ("Nintendo DS", r"\bnintendo ds\b"), ("PC", r"\bpc\b")]
NOISE = re.compile(r"\b(pegi|usk|ab \d+|freigegeben|nintendo|playstation|xbox|sony|microsoft|series|one|switch|ps[2-5]|"
                   r"edition|standard|deluxe|exclusive|only on|online|players?|spieler|game|videospiel|video game|"
                   r"blu-?ray|disc|new|neu|pc|dvd|rom)\b", re.I)


def detect_platform(text):
    low = (text or "").lower()
    return next((name for name, pattern in PLATFORM_WORDS if re.search(pattern, low)), None)


def clean_title(text):
    """Macht aus einem Produktnamen einen Spieletitel."""
    t = re.sub(r"[\[\(][^\]\)]*[\]\)]", " ", text or "")
    t = re.sub(r"\b(for|für)\b", " ", t, flags=re.I)
    for _, pattern in PLATFORM_WORDS:
        t = re.sub(pattern, " ", t, flags=re.I)
    t = NOISE.sub(" ", t)
    t = re.sub(r"\s*[-–|:,]+\s*$", "", re.sub(r"\s+", " ", t)).strip(" -–|:,")
    return t


def ocr_lines(image):
    """Liest das Foto in mehreren Varianten und liefert alle erkannten Zeilen."""
    gray = ImageOps.autocontrast(image.convert("L"))
    gray.thumbnail((1800, 1800))
    w, h = gray.size
    top = gray.crop((0, 0, w, h // 2))
    variants = [(gray, 11), (gray, 3), (ImageOps.invert(gray), 11), (top.resize((w * 2, h)), 11)]
    with ThreadPoolExecutor(2) as pool:
        texts = list(pool.map(_tesseract, variants))
    return [line for t in texts for line in t.splitlines()]


def _tesseract(job):
    image, psm = job
    buf = io.BytesIO()
    image.save(buf, "PNG")
    try:
        p = subprocess.run(["tesseract", "stdin", "stdout", "-l", "deu+eng", "--psm", str(psm)], input=buf.getvalue(),
                           capture_output=True, timeout=30)
        return p.stdout.decode("utf-8", "ignore")
    except (OSError, subprocess.TimeoutExpired):
        return ""


def candidates(lines):
    """Die vermutlich sinnvollen Suchbegriffe, beste zuerst."""
    seen, out = set(), []
    for line in lines:
        words = [w for w in re.findall(r"[A-Za-zÄÖÜäöüß0-9']+", NOISE.sub(" ", line)) if len(w) > 1 or w.isdigit()]
        words = [w for w in words if sum(c.isalpha() for c in w) >= 2 or w.isdigit()]
        text = " ".join(words)
        key = text.lower()
        letters = sum(c.isalpha() for c in text)
        if letters < 4 or key in seen or len(words) > 8:
            continue
        seen.add(key)
        out.append((letters, text))
    out.sort(key=lambda x: -x[0])
    return [t for _, t in out[:4]]


def _score(name, words):
    tokens = [t for t in re.findall(r"[a-z0-9äöüß]+", name.lower()) if len(t) > 1]
    if not tokens:
        return 0
    hits = sum(1 for t in tokens if difflib.get_close_matches(t, words, n=1, cutoff=0.8))
    return hits / len(tokens) + hits * 0.1


def recognise(lines):
    """Sucht zu den erkannten Zeilen Spiele und liefert die besten zuerst."""
    terms = candidates(lines)
    words = [w for w in re.findall(r"[a-z0-9äöüß]+", " ".join(lines).lower()) if len(w) > 1]
    found = {}
    for i, term in enumerate(terms):
        if i:
            time.sleep(0.3)  # IGDB erlaubt rund vier Anfragen pro Sekunde
        for game in igdb.search(term):
            found[game["ext_id"]] = game
    ranked = sorted(found.values(), key=lambda g: -_score(g["title"], words))
    return [g for g in ranked if _score(g["title"], words) > 0][:8], (terms[0] if terms else "")


def lookup_ean(ean):
    """Fragt eine freie Produktdatenbank nach dem Namen. Viele EU-Codes fehlen dort."""
    url = os.environ.get("UPC_BASE", "https://api.upcitemdb.com/prod/trial") + "/lookup"
    try:
        r = requests.get(url, params={"upc": ean}, headers={"Accept": "application/json"}, timeout=8)
        items = r.json().get("items") or [] if r.ok else []
    except (requests.RequestException, ValueError):
        return None
    return items[0].get("title") if items else None


def known_ean(ean):
    return db.get().execute("""SELECT e.media_id, e.platform, m.title, m.poster FROM ean_map e JOIN media m ON m.id=e.media_id
        WHERE e.ean=?""", (ean,)).fetchone()


def remember_ean(ean, media_id, platform):
    conn = db.get()
    conn.execute("INSERT OR REPLACE INTO ean_map(ean,media_id,platform) VALUES(?,?,?)", (ean, media_id, platform or None))
    conn.commit()
