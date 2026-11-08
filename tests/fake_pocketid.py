"""Nachbildung von Pocket ID für die Tests. Wird als opener an PocketID übergeben."""
import base64
import io
import json
import time
import urllib.parse

ISSUER = "https://id.test"


def _b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


class _Res(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FakePocketID:
    def __init__(self, client_id="client", client_secret="secret"):
        self.client_id, self.client_secret = client_id, client_secret
        self.user = {"sub": "sub-1", "email": "fin@example.com", "preferred_username": "fin", "name": "Fin",
                     "groups": ["familie"]}
        self.codes = {}  # code -> nonce
        self.requests = []

    def env(self, **extra):
        return {"OIDC_ISSUER": ISSUER, "OIDC_CLIENT_ID": self.client_id, "OIDC_CLIENT_SECRET": self.client_secret,
                **extra}

    def login(self, authorize_url, user=None):
        """Simuliert die Anmeldung im Browser: gibt die Parameter für den Rücksprung zurück."""
        if user:
            self.user = {**self.user, **user}
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(authorize_url).query))
        code = f"code-{len(self.codes)}"
        self.codes[code] = q["nonce"]
        return q["redirect_uri"], {"code": code, "state": q["state"]}

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.requests.append(url)
        if url.endswith("/.well-known/openid-configuration"):
            return _Res(json.dumps({"issuer": ISSUER, "authorization_endpoint": ISSUER + "/authorize",
                                    "token_endpoint": ISSUER + "/api/oidc/token",
                                    "userinfo_endpoint": ISSUER + "/api/oidc/userinfo"}).encode())
        if url.endswith("/api/oidc/token"):
            auth = base64.b64decode(req.get_header("Authorization").split()[1]).decode()
            assert auth == f"{self.client_id}:{self.client_secret}", auth
            form = dict(urllib.parse.parse_qsl(req.data.decode()))
            nonce = self.codes.pop(form["code"])
            assert form["code_verifier"]
            claims = {"iss": ISSUER, "aud": self.client_id, "exp": time.time() + 300, "nonce": nonce,
                      "sub": self.user["sub"]}
            token = f"{_b64({'alg': 'RS256'})}.{_b64(claims)}.sig"
            return _Res(json.dumps({"id_token": token, "access_token": "at-" + self.user["sub"]}).encode())
        if url.endswith("/api/oidc/userinfo"):
            return _Res(json.dumps(self.user).encode())
        raise AssertionError("unerwartete Anfrage " + url)
