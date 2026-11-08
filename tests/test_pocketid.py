import urllib.parse

from fake_pocketid import FakePocketID
from folgenreich import pocketid


def setup_oidc(app, **extra):
    fake = FakePocketID()
    app.extensions["pocketid"] = pocketid.PocketID(fake.env(**extra), opener=fake)
    return fake


def sign_in(app, fake, c, user=None, next_url="/"):
    res = c.get("/anmelden/pocketid?next=" + urllib.parse.quote(next_url))
    assert res.status_code == 302
    redirect, params = fake.login(res.headers["Location"], user)
    assert redirect == "http://localhost/anmelden/pocketid/zurueck"
    return c.get("/anmelden/pocketid/zurueck?" + urllib.parse.urlencode(params))


def test_existing_user_is_linked_by_username(env, client):
    fake = setup_oidc(env)
    c = env.test_client()
    assert "Mit Pocket ID anmelden" in c.get("/anmelden").get_data(as_text=True)
    res = sign_in(env, fake, c, {"preferred_username": "Nutzer1"}, "/serien")
    assert res.status_code == 302 and res.headers["Location"] == "/serien"
    assert c.get("/serien").status_code == 200
    from folgenreich import db
    with env.app_context():
        assert db.get().execute("SELECT oidc_sub FROM users WHERE id=1").fetchone()[0] == "sub-1"
    # Danach zählt die verknüpfte ID, auch wenn sich der Name in Pocket ID ändert
    c2 = env.test_client()
    assert sign_in(env, fake, c2, {"preferred_username": "anders"}).status_code == 302
    assert c2.get("/konto").status_code == 200


def test_unknown_user_is_rejected_unless_auto_create(env, client):
    fake = setup_oidc(env)
    c = env.test_client()
    res = sign_in(env, fake, c, {"sub": "x", "preferred_username": "fremd"})
    assert "fehler=1" in res.headers["Location"]
    assert "kein Konto" in c.get(res.headers["Location"]).get_data(as_text=True)
    fake = setup_oidc(env, OIDC_AUTO_CREATE="1")
    assert sign_in(env, fake, c, {"sub": "y", "preferred_username": "neu"}).headers["Location"] == "/"
    assert c.get("/konto").status_code == 200


def test_first_login_creates_admin_and_password_can_be_disabled(env):
    fake = setup_oidc(env, OIDC_DISABLE_PASSWORD="1", OIDC_AUTO_REDIRECT="1")
    c = env.test_client()
    assert c.get("/serien").headers["Location"].startswith("/anmelden")
    assert c.get("/anmelden").headers["Location"].endswith("/anmelden/pocketid")
    assert c.post("/anmelden", data={"username": "a", "password": "b"}).status_code == 403
    assert sign_in(env, fake, c).status_code == 302
    from folgenreich import db
    with env.app_context():
        assert db.get().execute("SELECT is_admin FROM users").fetchone()[0] == 1


def test_bad_state_is_rejected(env, client):
    fake = setup_oidc(env)
    c = env.test_client()
    res = c.get("/anmelden/pocketid")
    _, params = fake.login(res.headers["Location"])
    res = c.get("/anmelden/pocketid/zurueck?" + urllib.parse.urlencode({**params, "state": "falsch"}))
    assert "fehler=1" in res.headers["Location"]
    assert c.get("/serien").status_code == 302
