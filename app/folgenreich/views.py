import hmac
import json
import os
import re
import secrets
import time
import uuid
from collections import defaultdict
from functools import wraps

from flask import (Blueprint, Response, abort, current_app, flash, g, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import books, db, igdb, importer, jellyfin, jellyfin_history, logic, sync, tmdb

bp = Blueprint("main", __name__)
_fails = defaultdict(list)


@bp.before_app_request
def load_user():
    g.user = None
    if request.endpoint in ("main.health", "main.hook", "static"):
        return None
    if session.get("uid"):
        g.user = db.get().execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone()
    if request.method == "POST":  # Schutz gegen Anfragen von fremden Seiten
        origin = request.headers.get("Origin")
        if origin and origin.split("//")[-1] != request.host:
            abort(403)
    return None


def login_required(f):
    @wraps(f)
    def inner(*a, **k):
        if not g.user:
            if not db.get().execute("SELECT 1 FROM users LIMIT 1").fetchone():
                return redirect(url_for("main.setup"))
            return redirect(url_for("main.login"))
        return f(*a, **k)
    return inner


def admin_required(f):
    @wraps(f)
    def inner(*a, **k):
        if not g.user or not g.user["is_admin"]:
            abort(403)
        return f(*a, **k)
    return inner


@bp.app_context_processor
def ctx():
    return {"user": g.get("user"), "tmdb_ready": tmdb.configured()}


@bp.get("/health")
def health():
    db.get().execute("SELECT 1")
    return "ok"


# --- Anmeldung -----------------------------------------------------------------------------------

@bp.route("/einrichten", methods=["GET", "POST"])
def setup():
    if db.get().execute("SELECT 1 FROM users LIMIT 1").fetchone():
        return redirect(url_for("main.login"))
    if request.method == "POST":
        name, pw = request.form["username"].strip(), request.form["password"]
        if not name or len(pw) < 8:
            flash("Wähle einen Namen und ein Passwort mit mindestens 8 Zeichen.")
        else:
            conn = db.get()
            cur = conn.execute("INSERT INTO users(username,pw_hash,is_admin) VALUES(?,?,1)",
                               (name, generate_password_hash(pw)))
            conn.commit()
            session.permanent = True
            session["uid"] = cur.lastrowid
            return redirect(url_for("main.account") + "#umzug")
    return render_template("login.html", first=True)


@bp.route("/anmelden", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("main.start"))
    if not db.get().execute("SELECT 1 FROM users LIMIT 1").fetchone():
        return redirect(url_for("main.setup"))
    if request.method == "POST":
        ip = request.remote_addr or ""
        recent = [t for t in _fails[ip] if t > time.time() - 600]
        _fails[ip] = recent
        if len(recent) >= 8:
            flash("Zu viele Fehlversuche. Versuche es in zehn Minuten erneut.")
            return render_template("login.html"), 429
        row = db.get().execute("SELECT * FROM users WHERE username=?", (request.form["username"].strip(),)).fetchone()
        if row and check_password_hash(row["pw_hash"], request.form["password"]):
            session.permanent = True
            session["uid"] = row["id"]
            return redirect(url_for("main.start"))
        _fails[ip].append(time.time())
        flash("Name oder Passwort stimmt nicht.")
    return render_template("login.html")


@bp.post("/abmelden")
def logout():
    session.clear()
    return redirect(url_for("main.login"))


# --- Listen --------------------------------------------------------------------------------------

@bp.get("/")
@login_required
def start():
    uid = g.user["id"]
    games_all = logic.game_items(uid)
    series = logic.tv_tabs(uid, False)
    anime = logic.tv_tabs(uid, True)
    active = [i for i in series["aktiv"] + anime["aktiv"] if i["sub"] != "paused"]
    active.sort(key=lambda i: (not i["fresh"], logic._rev(i["last_activity"])))
    bks = logic.plain_items(uid, "book")
    movies = logic.plain_items(uid, "movie")
    return render_template("start.html", hero=active[0] if active else None, active=active[1:7],
                           more=max(0, len(active) - 7), soon=logic.upcoming(uid),
                           reading=[b for b in bks if b["state"] == "reading"],
                           playing=[x for x in games_all if x["state"] == "playing"],
                           empty=not (active or bks or movies or games_all or any(series.values()) or any(anime.values())))


TAB_LABELS = {"aktiv": "Aktiv", "warten": "Warten", "archiv": "Archiv", "merkliste": "Merkliste"}


def _tv_page(anime, tab, base):
    if tab not in TAB_LABELS:
        abort(404)
    tabs = logic.tv_tabs(g.user["id"], anime)
    stats = None
    if tab == "archiv":
        ids = [i["id"] for i in tabs["archiv"]]
        stats = {"titles": len(ids), "eps": sum(i["watched"] for i in tabs["archiv"])}
    return render_template("shelf.html", tabs=tabs, tab=tab, labels=TAB_LABELS, base=base, stats=stats,
                           heading="Anime" if anime else "Serien", items=tabs[tab])


@bp.get("/serien")
@bp.get("/serien/<tab>")
@login_required
def series(tab="aktiv"):
    return _tv_page(False, tab, "/serien")


@bp.get("/anime")
@bp.get("/anime/<tab>")
@login_required
def anime(tab="aktiv"):
    return _tv_page(True, tab, "/anime")


@bp.get("/filme")
@bp.get("/filme/<tab>")
@login_required
def movies(tab="merkliste"):
    items = logic.plain_items(g.user["id"], "movie")
    tabs = {"merkliste": [i for i in items if i["state"] != "done"], "gesehen": [i for i in items if i["state"] == "done"]}
    if tab not in tabs:
        abort(404)
    tabs["merkliste"].sort(key=lambda i: (i["release_date"] or "") <= logic.today())
    return render_template("plain.html", tabs=tabs, tab=tab, base="/filme", heading="Filme",
                           labels={"merkliste": "Merkliste", "gesehen": "Gesehen"}, items=tabs[tab])


@bp.get("/buecher")
@bp.get("/buecher/<tab>")
@login_required
def book_list(tab="regal"):
    items = logic.plain_items(g.user["id"], "book")
    items.sort(key=lambda b: (b["authors"] or "~").split(",")[0].split()[-1].lower() if b["authors"] else "~")
    tabs = {"regal": [b for b in items if b["owned"]], "lese": [b for b in items if b["state"] == "reading"],
            "gelesen": [b for b in items if b["state"] == "done"],
            "merkliste": [b for b in items if b["state"] == "planned" and not b["owned"]]}
    if tab not in tabs:
        abort(404)
    return render_template("plain.html", tabs=tabs, tab=tab, base="/buecher", heading="Bücher", books=True,
                           labels={"regal": "Mein Regal", "lese": "Lese ich", "gelesen": "Gelesen", "merkliste": "Merkliste"},
                           items=tabs[tab], unread=sum(1 for b in tabs["regal"] if b["state"] != "done"))


# --- Detailseite ---------------------------------------------------------------------------------

@bp.get("/spiele")
@bp.get("/spiele/<tab>")
@login_required
def games(tab="sammlung"):
    uid = g.user["id"]
    if tab == "statistik":
        return render_template("game_stats.html", s=logic.game_stats(uid), tabs_data=_game_tabs(uid), tab=tab)
    tabs = _game_tabs(uid)
    if tab not in tabs:
        abort(404)
    platforms = sorted({p for i in tabs["sammlung"] for p in (i["platforms"] or "").split("|") if p})
    return render_template("games.html", tabs=tabs, tab=tab, items=tabs[tab], platforms=platforms)


def _game_tabs(uid):
    items = logic.game_items(uid)
    return {"sammlung": [i for i in items if i["n_copies"]], "spiele": [i for i in items if i["state"] == "playing"],
            "durch": [i for i in items if i["state"] == "done"], "wunschliste": [i for i in items if i["state"] == "planned"]}


@bp.get("/spiel-foto")
@login_required
def game_photo():
    return render_template("game_photo.html", platforms=logic.known_platforms(g.user["id"]), formats=logic.FORMATS,
                           igdb_ready=igdb.configured())


@bp.post("/api/spiel-foto")
@login_required
def api_game_photo():
    f = request.files.get("photo")
    if not f:
        return jsonify(error="Es kam kein Foto an."), 400
    try:
        query = books.query_from_text(books.ocr(books.load_photo(f.read())))
    except Exception:
        return jsonify(error="Dieses Bildformat lässt sich nicht lesen. Versuche ein JPEG."), 400
    results, error = [], None
    if query:
        try:
            results = _decorate_games(igdb.search(query))
        except igdb.IgdbError as e:
            error = str(e)
    return render_template("_game_result.html", results=results, query=query, error=error,
                           formats=logic.FORMATS)


@bp.post("/spiel-von-hand")
@login_required
def game_manual():
    title = request.form.get("title", "").strip()
    if not title or not request.form.get("platform", "").strip():
        flash("Trage mindestens Titel und Plattform ein.")
        return redirect(url_for("main.game_photo"))
    data = {"title": title, "genres": [x.strip() for x in request.form.get("genres", "").split(",") if x.strip()],
            "year": request.form.get("year", type=int)}
    data["cover"] = _save_cover(request.files.get("photo"))
    return redirect(url_for("main.detail", media_id=_add_game(data, "sammlung", request.form)) + "#exemplare")


@bp.post("/m/<int:media_id>/exemplar")
@bp.post("/m/<int:media_id>/exemplar/<int:copy_id>")
@login_required
def save_copy(media_id, copy_id=None):
    logic.ensure_entry(g.user["id"], media_id, "backlog")
    if not logic.save_copy(g.user["id"], media_id, request.form, copy_id):
        flash("Wähle eine Plattform aus.")
    return redirect(url_for("main.detail", media_id=media_id) + "#exemplare")


@bp.post("/m/<int:media_id>/exemplar/<int:copy_id>/entfernen")
@login_required
def delete_copy(media_id, copy_id):
    conn = db.get()
    conn.execute("DELETE FROM copies WHERE id=? AND user_id=?", (copy_id, g.user["id"]))
    conn.commit()
    return redirect(url_for("main.detail", media_id=media_id) + "#exemplare")


def _media(media_id):
    m = db.get().execute("SELECT * FROM media WHERE id=?", (media_id,)).fetchone()
    if not m:
        abort(404)
    return m


@bp.get("/m/<int:media_id>")
@login_required
def detail(media_id):
    m = _media(media_id)
    uid = g.user["id"]
    entry = db.get().execute("SELECT * FROM entries WHERE user_id=? AND media_id=?", (uid, media_id)).fetchone()
    it, seasons, open_season = None, [], None
    if m["type"] == "tv":
        seasons = logic.seasons(uid, media_id)
        rows = logic.tv_items(uid, media_id)
        if rows:
            it = rows[0]
            it["user_id"] = uid
            logic.attach_next([it])
            it["label"] = logic.human_tv_status(it)
        open_season = request.args.get("s", type=int)
        if open_season is None and it and it["next_season"] is not None:
            open_season = it["next_season"]
    game = {}
    if m["type"] == "game":
        game = {"copies": logic.copies(uid, media_id), "platforms": logic.known_platforms(uid), "formats": logic.FORMATS,
                "conditions": logic.CONDITIONS, "states": logic.GAME_STATE}
    return render_template("detail.html", m=m, entry=entry, it=it, seasons=seasons, open_season=open_season,
                           today=logic.today(), **game)


def _card_response(media_id, undo=None):
    rows = logic.tv_items(g.user["id"], media_id)
    it = rows[0]
    it["user_id"] = g.user["id"]
    logic.attach_next([it])
    it["fresh"] = False
    return jsonify(tab=it["tab"], undo=undo, label=logic.pos_label(undo) if undo else None,
                   open=it["open_eps"], status=logic.human_tv_status(it),
                   html=render_template("_card.html", it=it) if it["tab"] == "aktiv" else "")


@bp.post("/api/m/<int:media_id>/next")
@login_required
def api_next(media_id):
    pos = logic.mark_next(g.user["id"], media_id)
    if not pos:
        return jsonify(error="Keine offene Folge."), 409
    return _card_response(media_id, pos)


@bp.post("/api/m/<int:media_id>/ep")
@login_required
def api_ep(media_id):
    d = request.get_json()
    logic.set_watched(g.user["id"], media_id, [(int(d["season"]), int(d["number"]))], bool(d.get("on", True)))
    return _card_response(media_id)


@bp.post("/api/m/<int:media_id>/until")
@login_required
def api_until(media_id):
    logic.set_watched(g.user["id"], media_id, logic.aired_episodes(media_id, upto=int(request.get_json()["pos"])))
    return _card_response(media_id)


@bp.post("/api/m/<int:media_id>/season")
@login_required
def api_season(media_id):
    d = request.get_json()
    eps = logic.aired_episodes(media_id, season=int(d["season"]))
    logic.set_watched(g.user["id"], media_id, eps, bool(d.get("on", True)))
    return _card_response(media_id)


@bp.post("/m/<int:media_id>/status")
@login_required
def set_status(media_id):
    m = _media(media_id)
    state = request.form["state"]
    allowed = {"tv": {"follow", "planned", "paused", "dropped", "archived"}, "movie": {"planned", "done"},
               "book": {"planned", "reading", "done"},
               "game": set(logic.GAME_STATE)}[m["type"]]
    if state not in allowed:
        abort(400)
    logic.set_state(g.user["id"], media_id, state)
    return redirect(url_for("main.detail", media_id=media_id))


@bp.post("/m/<int:media_id>/alles")
@login_required
def mark_all(media_id):
    logic.set_watched(g.user["id"], media_id, logic.aired_episodes(media_id))
    return redirect(url_for("main.detail", media_id=media_id))


@bp.post("/m/<int:media_id>/notiz")
@login_required
def save_note(media_id):
    raw = request.form.get("rating", "").replace(",", ".").strip()
    try:
        rating = max(0.0, min(10.0, float(raw))) if raw else None
    except ValueError:
        rating = None
    logic.ensure_entry(g.user["id"], media_id, "planned")
    conn = db.get()
    conn.execute("UPDATE entries SET rating=?, notes=? WHERE user_id=? AND media_id=?",
                 (rating, request.form.get("notes", "").strip() or None, g.user["id"], media_id))
    conn.commit()
    flash("Gespeichert.")
    return redirect(url_for("main.detail", media_id=media_id))


@bp.post("/m/<int:media_id>/regal")
@login_required
def toggle_owned(media_id):
    logic.ensure_entry(g.user["id"], media_id, "planned")
    conn = db.get()
    conn.execute("UPDATE entries SET owned=1-owned WHERE user_id=? AND media_id=?", (g.user["id"], media_id))
    conn.commit()
    return redirect(url_for("main.detail", media_id=media_id))


@bp.post("/m/<int:media_id>/art")
@login_required
def toggle_anime(media_id):
    conn = db.get()
    conn.execute("UPDATE media SET anime=1-anime, anime_locked=1 WHERE id=?", (media_id,))
    conn.commit()
    return redirect(url_for("main.detail", media_id=media_id))


@bp.post("/m/<int:media_id>/cover")
@login_required
def set_cover(media_id):
    m = _media(media_id)
    if m["type"] not in ("book", "game"):
        abort(400)
    if request.files.get("photo") and request.files["photo"].filename:
        cover_url = _save_cover(request.files["photo"])
    else:
        isbn = books.clean_isbn(m["ext_id"]) if m["type"] == "book" else None
        cover_url = books.best_cover(isbn, m["poster"] if (m["poster"] or "").startswith("/cover/") else None)
        if not cover_url:
            flash("Für dieses Buch ist kein Cover zu finden. Lade ein eigenes Foto hoch.")
    if cover_url:
        conn = db.get()
        conn.execute("UPDATE media SET poster=? WHERE id=?", (cover_url, media_id))
        conn.commit()
        flash("Cover aktualisiert.")
    return redirect(url_for("main.detail", media_id=media_id))


@bp.post("/m/<int:media_id>/entfernen")
@login_required
def remove(media_id):
    m = _media(media_id)
    logic.remove_entry(g.user["id"], media_id)
    flash(f"{m['title']} wurde entfernt.")
    return redirect(url_for("main.start"))


# --- Suchen und Hinzufügen -----------------------------------------------------------------------

def _mine(type_, ext_id):
    return db.get().execute("""SELECT m.id, e.state, e.owned FROM media m JOIN entries e ON e.media_id=m.id AND e.user_id=?
        WHERE m.type=? AND m.ext_id=?""", (g.user["id"], type_, str(ext_id))).fetchone()


def _book_key(b):
    if b.get("isbn"):
        return b["isbn"]
    return "x" + re.sub(r"\W", "", f"{b.get('title')}{b.get('authors')}".lower())[:60]


def _decorate_books(results):
    for b in results:
        b["mine"] = _mine("book", _book_key(b))
        b["payload"] = json.dumps(b | {"mine": None}, ensure_ascii=False)
    return results


def _decorate_games(results):
    uid = g.user["id"]
    for r in results:
        mine = _mine("game", r["ext_id"])
        r["mine"] = mine
        r["have"] = sorted({c["platform"] for c in logic.copies(uid, mine["id"])}) if mine else []
        r["payload"] = json.dumps(r | {"mine": None, "have": None}, ensure_ascii=False)
    return results


@bp.get("/suche")
@login_required
def search():
    q = request.args.get("q", "").strip()
    art = request.args.get("art", "medien")
    results, error = [], None
    if q and art == "buch":
        results = _decorate_books(books.search(q))
    elif q and art == "spiel":
        try:
            results = _decorate_games(igdb.search(q))
        except igdb.IgdbError as e:
            error = str(e)
    elif q:
        try:
            results = tmdb.search(q)
            for r in results:
                r["mine"] = _mine(r["type"], r["ext_id"])
        except tmdb.TmdbError as e:
            error = str(e)
    return render_template("search.html", q=q, art=art, results=results, error=error,
                           igdb_ready=igdb.configured(), formats=logic.FORMATS)


@bp.post("/hinzufuegen")
@login_required
def add():
    action, uid = request.form["action"], g.user["id"]
    if request.form["type"] == "book":
        b = json.loads(request.form["payload"])
        return redirect(url_for("main.detail", media_id=_add_book(b, action)))
    if request.form["type"] == "game":
        media_id = _add_game(json.loads(request.form["payload"]), action, request.form)
        return redirect(url_for("main.detail", media_id=media_id) + "#exemplare")
    type_, ext_id = request.form["type"], request.form["ext_id"]
    media_id = sync.get_or_create(type_, ext_id, request.form.get("title"), request.form.get("poster") or None)
    try:
        sync.sync_media(media_id)
    except tmdb.TmdbError as e:
        flash(f"Eingetragen, Details folgen später: {e}")
    state = {"merken": "planned", "schaue": "follow", "gesehen": "done" if type_ == "movie" else "follow"}[action]
    logic.ensure_entry(uid, media_id, state)
    if action == "gesehen" and type_ == "tv":
        logic.set_watched(uid, media_id, logic.aired_episodes(media_id))
    elif action == "gesehen":
        logic.set_state(uid, media_id, "done")
    db.get().commit()
    return redirect(url_for("main.detail", media_id=media_id))


def _add_game(data, action, form):
    uid = g.user["id"]
    conn = db.get()
    key = data.get("ext_id") or "x" + uuid.uuid4().hex
    conn.execute("""INSERT OR IGNORE INTO media(type,ext_id,source,title,year,poster,overview,genres,status)
        VALUES('game',?,?,?,?,?,?,?,'released')""",
                 (key, "igdb" if data.get("ext_id") else "manual", data["title"], data.get("year"), data.get("cover"),
                  data.get("overview"), ", ".join(data.get("genres") or []) or None))
    media_id = conn.execute("SELECT id FROM media WHERE type='game' AND ext_id=?", (key,)).fetchone()["id"]
    logic.ensure_entry(uid, media_id, "planned" if action == "merken" else "backlog")
    conn.commit()
    if action == "sammlung":
        logic.save_copy(uid, media_id, form)
    return media_id


def _add_book(b, action):
    uid = g.user["id"]
    conn = db.get()
    key = _book_key(b)
    pages = b.get("pages") if isinstance(b.get("pages"), int) else None
    if not (b.get("cover") or "").startswith("/cover/"):
        b["cover"] = books.best_cover(b.get("isbn"), b.get("cover"))
    conn.execute("""INSERT OR IGNORE INTO media(type,ext_id,source,title,year,poster,overview,authors,publisher,pages,status)
        VALUES('book',?,?,?,?,?,?,?,?,?,'released')""",
                 (key, "books" if b.get("isbn") else "manual", b["title"], b.get("year"), b.get("cover"),
                  b.get("overview"), b.get("authors"), b.get("publisher"), pages))
    media_id = conn.execute("SELECT id FROM media WHERE type='book' AND ext_id=?", (key,)).fetchone()["id"]
    state = {"merken": "planned", "regal": "planned", "gelesen": "done", "lese": "reading"}[action]
    logic.ensure_entry(uid, media_id, state)
    if action == "regal":
        conn.execute("UPDATE entries SET owned=1 WHERE user_id=? AND media_id=?", (uid, media_id))
    elif action == "gelesen":
        logic.set_state(uid, media_id, "done")
        conn.execute("UPDATE entries SET owned=? WHERE user_id=? AND media_id=?",
                     (1 if request.form.get("owned") else 0, uid, media_id))
    conn.commit()
    return media_id


@bp.get("/buch-foto")
@login_required
def book_photo():
    return render_template("photo.html")


@bp.post("/api/buch-foto")
@login_required
def api_book_photo():
    isbn = books.clean_isbn(request.form.get("isbn"))
    how, query = "barcode", None
    if not isbn:
        f = request.files.get("photo")
        if not f:
            return jsonify(error="Es kam kein Foto an."), 400
        try:
            res = books.analyse_photo(f.read())
        except Exception:
            return jsonify(error="Dieses Bildformat lässt sich nicht lesen. Versuche ein JPEG."), 400
        isbn, how, query = res["isbn"], res["how"], res.get("query")
    results = []
    if isbn:
        hit = books.by_isbn(isbn)
        results = [hit] if hit else []
    elif query:
        results = books.search(query)
    return render_template("_photo_result.html", results=_decorate_books(results), isbn=isbn, how=how, query=query)


def _save_cover(f):
    if not f or not f.filename:
        return None
    try:
        img = books.load_photo(f.read())
        img.thumbnail((700, 1000))
        name = uuid.uuid4().hex + ".jpg"
        img.save(os.path.join(db.data_dir(), "covers", name), "JPEG", quality=84)
        return "/cover/" + name
    except Exception:
        flash("Das Foto ließ sich nicht verwenden. Der Eintrag bleibt ohne Cover.")
        return None


@bp.post("/buch-von-hand")
@login_required
def book_manual():
    title = request.form.get("title", "").strip()
    if not title:
        flash("Trage mindestens den Titel ein.")
        return redirect(url_for("main.book_photo"))
    b = {"title": title, "authors": request.form.get("authors", "").strip() or None,
         "isbn": books.clean_isbn(request.form.get("isbn"))}
    b["cover"] = _save_cover(request.files.get("photo"))
    return redirect(url_for("main.detail", media_id=_add_book(b, request.form.get("action", "regal"))))


@bp.get("/cover/<name>")
@login_required
def cover(name):
    return send_from_directory(os.path.join(db.data_dir(), "covers"), name, max_age=86400 * 30)


# --- Konto und Verwaltung ------------------------------------------------------------------------

def _hook_token():
    token = db.setting("hook_token")
    if not token:
        token = secrets.token_urlsafe(24)
        db.set_setting("hook_token", token)
    return token


@bp.get("/konto")
@login_required
def account():
    conn = db.get()
    data = {}
    if g.user["is_admin"]:
        data = {"users": conn.execute("SELECT * FROM users ORDER BY username").fetchall(),
                "hook_url": request.host_url.rstrip("/") + url_for("main.hook", token=_hook_token()),
                "log": conn.execute("SELECT * FROM hook_log ORDER BY id DESC LIMIT 12").fetchall(),
                "counts": conn.execute("""SELECT COUNT(*) AS total, SUM(synced_at IS NULL) AS waiting,
                    SUM(sync_error IS NOT NULL) AS errors FROM media WHERE source='tmdb'""").fetchone(),
                "last_error": conn.execute("SELECT title,sync_error FROM media WHERE sync_error IS NOT NULL LIMIT 1").fetchone()}
    data["jf"] = jellyfin_history.status(g.user["id"])
    data["jf_ready"] = jellyfin_history.configured()
    return render_template("account.html", **data)


@bp.post("/konto/profil")
@login_required
def save_profile():
    conn = db.get()
    conn.execute("UPDATE users SET jellyfin_name=? WHERE id=?",
                 (request.form.get("jellyfin_name", "").strip() or None, g.user["id"]))
    conn.commit()
    flash("Gespeichert.")
    return redirect(url_for("main.account"))


@bp.post("/konto/jellyfin/pruefen")
@login_required
def jellyfin_check():
    if jellyfin_history.configured():
        name = g.user["jellyfin_name"] or g.user["username"]
        jellyfin_history.start(g.user["id"], name, background=current_app.config.get("BACKGROUND", True))
    return redirect(url_for("main.account") + "#jellyfin")


@bp.post("/konto/jellyfin/uebernehmen")
@login_required
def jellyfin_apply():
    done = jellyfin_history.apply(g.user["id"])
    if done:
        flash(f"Aus Jellyfin ergänzt: {done['episodes']} Folgen und {len(done['movies'])} Filme.")
    return redirect(url_for("main.account") + "#jellyfin")


@bp.post("/konto/jellyfin/verwerfen")
@login_required
def jellyfin_discard():
    jellyfin_history.discard(g.user["id"])
    return redirect(url_for("main.account") + "#jellyfin")


@bp.post("/konto/passwort")
@login_required
def save_password():
    if not check_password_hash(g.user["pw_hash"], request.form["old"]):
        flash("Das aktuelle Passwort stimmt nicht.")
    elif len(request.form["new"]) < 8:
        flash("Das neue Passwort braucht mindestens 8 Zeichen.")
    else:
        conn = db.get()
        conn.execute("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(request.form["new"]), g.user["id"]))
        conn.commit()
        flash("Passwort geändert.")
    return redirect(url_for("main.account"))


@bp.post("/konto/import")
@login_required
def do_import():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Wähle zuerst die CSV-Datei aus.")
        return redirect(url_for("main.account") + "#umzug")
    try:
        res, skipped = importer.run(f.read().decode("utf-8-sig"), g.user["id"])
    except (ValueError, UnicodeDecodeError, KeyError) as e:
        flash(f"Import fehlgeschlagen: {e}")
        return redirect(url_for("main.account") + "#umzug")
    msg = (f"Übernommen: {res.get('serien', 0)} Serien, {res.get('filme', 0)} Filme und "
           f"{res.get('folgen', 0):,} gesehene Folgen.".replace(",", "."))
    if skipped:
        msg += " Nicht übernommen: " + ", ".join(f"{v}× {k}" for k, v in skipped.items()) + "."
    flash(msg)
    return redirect(url_for("main.series"))


@bp.get("/konto/export.json")
@login_required
def export():
    conn = db.get()
    uid = g.user["id"]
    entries = [dict(r) for r in conn.execute("""SELECT m.type,m.ext_id,m.title,m.authors,e.state,e.owned,e.rating,e.notes,
        e.added_at,e.finished_at,e.last_activity FROM entries e JOIN media m ON m.id=e.media_id WHERE e.user_id=?""", (uid,))]
    watches = [dict(r) for r in conn.execute("""SELECT m.ext_id,m.title,w.season,w.number,w.watched_at,w.plays
        FROM watches w JOIN media m ON m.id=w.media_id WHERE w.user_id=?""", (uid,))]
    owned = [dict(r) for r in conn.execute("""SELECT m.title,c.platform,c.format,c.edition,c.condition,c.complete,c.price_cents,
        c.bought_on,c.location FROM copies c JOIN media m ON m.id=c.media_id WHERE c.user_id=?""", (uid,))]
    body = json.dumps({"entries": entries, "watches": watches, "games": owned}, ensure_ascii=False, indent=1)
    return Response(body, mimetype="application/json",
                    headers={"Content-Disposition": "attachment; filename=folgenreich-export.json"})


@bp.post("/konto/nutzer")
@login_required
@admin_required
def add_user():
    name = request.form["username"].strip()
    pw = secrets.token_urlsafe(9)
    conn = db.get()
    try:
        conn.execute("INSERT INTO users(username,pw_hash) VALUES(?,?)", (name, generate_password_hash(pw)))
        conn.commit()
        flash(f"Konto für {name} angelegt. Erstes Passwort: {pw}")
    except Exception:
        flash("Diesen Namen gibt es schon.")
    return redirect(url_for("main.account"))


@bp.post("/konto/nutzer/<int:user_id>/passwort")
@login_required
@admin_required
def reset_user(user_id):
    pw = secrets.token_urlsafe(9)
    conn = db.get()
    conn.execute("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(pw), user_id))
    conn.commit()
    flash(f"Neues Passwort: {pw}")
    return redirect(url_for("main.account"))


@bp.post("/konto/nutzer/<int:user_id>/loeschen")
@login_required
@admin_required
def delete_user(user_id):
    if user_id != g.user["id"]:
        conn = db.get()
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()
        flash("Konto gelöscht.")
    return redirect(url_for("main.account"))


@bp.post("/konto/abgleich")
@login_required
@admin_required
def kick_sync():
    conn = db.get()
    conn.execute("UPDATE media SET synced_at=NULL, sync_error=NULL WHERE sync_error IS NOT NULL")
    conn.commit()
    sync.kick()
    flash("Der Abgleich wurde gestartet.")
    return redirect(url_for("main.account"))


@bp.post("/hook/jellyfin/<token>")
def hook(token):
    if not hmac.compare_digest(token, _hook_token()):
        abort(404)
    payload = request.get_json(force=True, silent=True) or {}
    return jsonify(result=jellyfin.handle(payload))


@bp.get("/manifest.webmanifest")
def manifest():
    return jsonify(name="Folgenreich", short_name="Folgenreich", start_url="/", display="standalone",
                   background_color="#1c1830", theme_color="#1c1830",
                   icons=[{"src": url_for("static", filename="icon-192.png"), "sizes": "192x192", "type": "image/png"},
                          {"src": url_for("static", filename="icon-512.png"), "sizes": "512x512", "type": "image/png"}])
