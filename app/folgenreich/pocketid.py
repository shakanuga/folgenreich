"""Anmeldung über Pocket ID (OpenID Connect).

Nur Python-Standardbibliothek, damit die Datei in jede App passt. Ablauf: Authorization Code Flow mit
PKCE, state und nonce. Das Token kommt direkt vom Token-Endpunkt über HTTPS, deshalb prüfen wir die
Angaben im ID-Token (Aussteller, Empfänger, Ablauf, nonce) und holen den Rest vom Userinfo-Endpunkt.

Einstellungen über Umgebungsvariablen:
  OIDC_ISSUER            z. B. https://id.unheim.de (Pflicht)
  OIDC_CLIENT_ID         aus Pocket ID (Pflicht)
  OIDC_CLIENT_SECRET     aus Pocket ID (Pflicht)
  OIDC_REDIRECT_URI      optional, sonst aus der aufgerufenen Adresse gebildet
  OIDC_LABEL             Text auf dem Knopf, Standard "Mit Pocket ID anmelden"
  OIDC_AUTO_REDIRECT     1 = Anmeldeseite überspringen und direkt zu Pocket ID
  OIDC_DISABLE_PASSWORD  1 = Anmeldung mit Passwort abschalten (nur wo es Passwörter gibt)
  OIDC_AUTO_CREATE       1 = unbekannte Nutzer automatisch anlegen (nur wo es Konten gibt)
  OIDC_ALLOWED_GROUPS    optional, kommagetrennt: nur Mitglieder dieser Gruppen dürfen rein
  OIDC_ADMIN_GROUPS      optional, kommagetrennt: Mitglieder werden beim Anlegen Admin
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

PENDING_COOKIE = "oidc_pending"
PENDING_MAX_AGE = 600
SCOPE = "openid email profile groups"


class OIDCError(Exception):
    """Fehler, dessen Text dem Nutzer gezeigt werden darf."""


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _flag(env, name):
    return str(env.get(name, "")).strip().lower() in ("1", "true", "yes", "ja", "on")


def _list(env, name):
    return {p.strip() for p in str(env.get(name, "")).split(",") if p.strip()}


def secret_key(data_dir, name="oidc.key"):
    """Liest den Schlüssel für signierte Cookies oder legt ihn einmalig an."""
    path = os.path.join(str(data_dir), name)
    if not os.path.exists(path):
        os.makedirs(str(data_dir), exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(secrets.token_hex(32))
        except FileExistsError:
            pass
    with open(path) as f:
        return f.read().strip().encode()


def sign(data, key):
    """Packt ein JSON-fähiges Objekt in einen signierten String (für Cookies)."""
    body = _b64url(json.dumps(data, separators=(",", ":")).encode())
    mac = _b64url(hmac.new(key, body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"


def unsign(value, key, max_age=None):
    """Gegenstück zu sign(). Gibt None zurück, wenn der Wert fehlt, verändert oder zu alt ist."""
    if not value or "." not in value:
        return None
    body, mac = value.rsplit(".", 1)
    good = _b64url(hmac.new(key, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(mac, good):
        return None
    try:
        data = json.loads(_b64url_decode(body))
    except ValueError:
        return None
    if max_age is not None and (not isinstance(data, dict) or time.time() - data.get("t", 0) > max_age):
        return None
    return data


class PocketID:
    def __init__(self, env=None, opener=None):
        env = os.environ if env is None else env
        self.issuer = str(env.get("OIDC_ISSUER", "")).strip().rstrip("/")
        self.client_id = str(env.get("OIDC_CLIENT_ID", "")).strip()
        self.client_secret = str(env.get("OIDC_CLIENT_SECRET", "")).strip()
        self.redirect_uri = str(env.get("OIDC_REDIRECT_URI", "")).strip()
        self.label = str(env.get("OIDC_LABEL", "")).strip() or "Mit Pocket ID anmelden"
        self.auto_redirect = _flag(env, "OIDC_AUTO_REDIRECT")
        self.password_login = not _flag(env, "OIDC_DISABLE_PASSWORD")
        self.auto_create = _flag(env, "OIDC_AUTO_CREATE")
        self.allowed_groups = _list(env, "OIDC_ALLOWED_GROUPS")
        self.admin_groups = _list(env, "OIDC_ADMIN_GROUPS")
        self._open = opener or urllib.request.urlopen
        self._meta = None
        self._meta_at = 0.0

    @property
    def enabled(self):
        return bool(self.issuer and self.client_id and self.client_secret)

    def public(self):
        """Was die Oberfläche wissen muss."""
        return {"enabled": self.enabled, "label": self.label, "auto_redirect": self.enabled and self.auto_redirect,
                "password_login": self.password_login or not self.enabled}

    # --- HTTP -------------------------------------------------------------------------------------

    def _request(self, url, data=None, headers=None):
        req = urllib.request.Request(url, data=data, headers={"Accept": "application/json", **(headers or {})})
        try:
            with self._open(req, timeout=15) as res:
                return json.loads(res.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise OIDCError(f"Pocket ID antwortet mit Fehler {e.code}: {detail}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise OIDCError(f"Pocket ID ist nicht erreichbar ({e}). Stimmt OIDC_ISSUER?") from e

    def metadata(self):
        if self._meta is None or time.time() - self._meta_at > 3600:
            self._meta = self._request(self.issuer + "/.well-known/openid-configuration")
            self._meta_at = time.time()
        return self._meta

    # --- Ablauf -----------------------------------------------------------------------------------

    def begin(self, redirect_uri, next_url="/"):
        """Liefert (Adresse bei Pocket ID, Zwischenstand für das Cookie)."""
        if not self.enabled:
            raise OIDCError("Pocket ID ist nicht eingerichtet.")
        redirect_uri = self.redirect_uri or redirect_uri
        verifier = secrets.token_urlsafe(48)
        pending = {"state": secrets.token_urlsafe(24), "nonce": secrets.token_urlsafe(24), "verifier": verifier,
                   "redirect_uri": redirect_uri, "next": safe_next(next_url), "t": int(time.time())}
        params = {"response_type": "code", "client_id": self.client_id, "redirect_uri": redirect_uri,
                  "scope": SCOPE, "state": pending["state"], "nonce": pending["nonce"],
                  "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
                  "code_challenge_method": "S256"}
        endpoint = self.metadata()["authorization_endpoint"]
        sep = "&" if "?" in endpoint else "?"
        return endpoint + sep + urllib.parse.urlencode(params), pending

    def complete(self, params, pending):
        """Prüft die Rückkehr von Pocket ID und gibt die Nutzerdaten zurück."""
        if params.get("error"):
            raise OIDCError("Pocket ID hat die Anmeldung abgelehnt: "
                            + (params.get("error_description") or params["error"]))
        if not pending or not params.get("state") or not hmac.compare_digest(str(params["state"]), pending["state"]):
            raise OIDCError("Die Anmeldung ist abgelaufen oder kam nicht von hier. Bitte noch einmal versuchen.")
        if time.time() - pending.get("t", 0) > PENDING_MAX_AGE:
            raise OIDCError("Die Anmeldung hat zu lange gedauert. Bitte noch einmal versuchen.")
        if not params.get("code"):
            raise OIDCError("Pocket ID hat keinen Anmeldecode geschickt.")
        meta = self.metadata()
        basic = base64.b64encode(f"{urllib.parse.quote(self.client_id, safe='')}:"
                                 f"{urllib.parse.quote(self.client_secret, safe='')}".encode()).decode()
        body = urllib.parse.urlencode({"grant_type": "authorization_code", "code": params["code"],
                                       "redirect_uri": pending["redirect_uri"],
                                       "code_verifier": pending["verifier"]}).encode()
        tokens = self._request(meta["token_endpoint"], data=body,
                               headers={"Authorization": f"Basic {basic}",
                                        "Content-Type": "application/x-www-form-urlencoded"})
        claims = self._check_id_token(tokens.get("id_token"), meta, pending["nonce"])
        if tokens.get("access_token") and meta.get("userinfo_endpoint"):
            info = self._request(meta["userinfo_endpoint"],
                                 headers={"Authorization": f"Bearer {tokens['access_token']}"})
            if info.get("sub") != claims["sub"]:
                raise OIDCError("Pocket ID hat widersprüchliche Nutzerdaten geschickt.")
            claims = {**claims, **info}
        groups = claims.get("groups") or []
        if isinstance(groups, str):
            groups = [groups]
        if self.allowed_groups and not self.allowed_groups & set(groups):
            raise OIDCError("Dein Konto ist für diese App nicht freigeschaltet.")
        return {"sub": claims["sub"], "email": (claims.get("email") or "").strip(),
                "username": (claims.get("preferred_username") or "").strip(),
                "name": (claims.get("name") or claims.get("given_name") or "").strip(),
                "groups": list(groups), "is_admin": bool(self.admin_groups & set(groups))}

    def _check_id_token(self, id_token, meta, nonce):
        if not id_token or id_token.count(".") != 2:
            raise OIDCError("Pocket ID hat kein gültiges ID-Token geschickt.")
        try:
            claims = json.loads(_b64url_decode(id_token.split(".")[1]))
        except ValueError as e:
            raise OIDCError("Das ID-Token von Pocket ID ist nicht lesbar.") from e
        aud = claims.get("aud")
        auds = aud if isinstance(aud, list) else [aud]
        if claims.get("iss") != meta.get("issuer", self.issuer):
            raise OIDCError("Das ID-Token stammt von einem anderen Aussteller. Stimmt OIDC_ISSUER?")
        if self.client_id not in auds:
            raise OIDCError("Das ID-Token ist für eine andere App ausgestellt. Stimmt OIDC_CLIENT_ID?")
        if float(claims.get("exp", 0)) < time.time() - 60:
            raise OIDCError("Das ID-Token ist abgelaufen. Geht die Uhr des Servers richtig?")
        if not claims.get("nonce") or not hmac.compare_digest(str(claims["nonce"]), nonce):
            raise OIDCError("Das ID-Token passt nicht zu dieser Anmeldung. Bitte noch einmal versuchen.")
        if not claims.get("sub"):
            raise OIDCError("Pocket ID hat keine Nutzer-ID geschickt.")
        return claims


def safe_next(url):
    """Nur Pfade auf derselben Seite zulassen, keine fremden Adressen."""
    url = str(url or "/")
    if not url.startswith("/") or url.startswith("//") or "\\" in url:
        return "/"
    return url


def match_user(rows, info):
    """Sucht das passende bestehende Konto: erst über die verknüpfte Pocket-ID, dann über Benutzername
    oder E-Mail. rows: Liste von dicts mit id, oidc_sub, username und optional email."""
    for r in rows:
        if r.get("oidc_sub") and r["oidc_sub"] == info["sub"]:
            return r
    keys = {k.lower() for k in (info.get("username"), info.get("email")) if k}
    for r in rows:
        if r.get("oidc_sub"):
            continue  # schon mit einem anderen Pocket-ID-Konto verknüpft
        own = {str(r.get(k) or "").lower() for k in ("username", "email")} - {""}
        if own & keys:
            return r
    return None
