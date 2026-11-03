"""Folgenreich: Serien, Filme, Animes und Bücher im Blick behalten."""
import os
import secrets
from datetime import timedelta

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from . import db, logic, sync


def _secret():
    p = os.path.join(db.data_dir(), "secret.key")
    if not os.path.exists(p):
        fd = os.open(p, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    with open(p) as f:
        return f.read().strip()


def create_app(background=True):
    db.init()
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config.update(SECRET_KEY=_secret(), MAX_CONTENT_LENGTH=32 * 1024 * 1024,
                      SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
                      PERMANENT_SESSION_LIFETIME=timedelta(days=120))
    app.jinja_env.filters["dedate"] = logic.de_date
    app.jinja_env.filters["num"] = lambda n: f"{n:,}".replace(",", ".")
    app.jinja_env.filters["score"] = lambda r: f"{r:g}".replace(".", ",") if r is not None else ""
    app.jinja_env.filters["euro"] = lambda c: f"{c / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " €"
    app.jinja_env.globals["pos_label"] = logic.pos_label
    app.jinja_env.globals["waiting_label"] = logic.waiting_label
    app.config["BACKGROUND"] = background
    from .views import bp
    app.register_blueprint(bp)
    if background:
        sync.start()
    return app
