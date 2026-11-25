"""Mock OIDC provider issuing Cognito-shaped tokens, for local and CI end-to-end tests only.

Implements just enough for the web app (oidc-client-ts, authorization code + PKCE S256) and
the API (JWKS verification): discovery, authorize (auto-approves), token, JWKS, logout.
Never deploy this: it signs in anyone who asks.

Run:  MOCK_OIDC_ISSUER=http://127.0.0.1:9100 uvicorn server:app --app-dir tools/mock-oidc --port 9100
Users: pass ?login_hint=<sub> to /oauth2/authorize (default e2e-user); tenant "e2e".
"""

import base64
import hashlib
import json
import os
import secrets
import time
from urllib.parse import urlencode

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

ISSUER = os.getenv("MOCK_OIDC_ISSUER", "http://127.0.0.1:9100").rstrip("/")
CLIENT_ID = os.getenv("MOCK_OIDC_CLIENT_ID", "e2e-web-client")
TENANT = os.getenv("MOCK_OIDC_TENANT", "e2e")

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_key.public_key()))
_jwk.update({"kid": "mock-1", "alg": "RS256", "use": "sig"})
_codes: dict[str, dict] = {}

app = FastAPI(title="mock-oidc (test only)")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])


@app.get("/.well-known/openid-configuration")
def discovery():
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/oauth2/authorize",
        "token_endpoint": f"{ISSUER}/oauth2/token",
        "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["openid", "email"],
    }


@app.get("/.well-known/jwks.json")
def jwks():
    return {"keys": [_jwk]}


@app.get("/oauth2/authorize")
def authorize(
    client_id: str,
    redirect_uri: str,
    response_type: str,
    code_challenge: str,
    code_challenge_method: str,
    state: str = "",
    nonce: str = "",
    login_hint: str = "e2e-user",
    scope: str = "openid",
):
    if client_id != CLIENT_ID or response_type != "code" or code_challenge_method != "S256":
        raise HTTPException(400, "invalid_request")
    code = secrets.token_urlsafe(24)
    _codes[code] = {"challenge": code_challenge, "redirect_uri": redirect_uri, "nonce": nonce, "sub": login_hint}
    return RedirectResponse(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}", status_code=302)


def _sign(claims: dict) -> str:
    return jwt.encode(claims, _key, algorithm="RS256", headers={"kid": "mock-1"})


@app.post("/oauth2/token")
def token(
    grant_type: str = Form(...),
    code: str = Form(...),
    redirect_uri: str = Form(...),
    client_id: str = Form(...),
    code_verifier: str = Form(...),
):
    entry = _codes.pop(code, None)
    if grant_type != "authorization_code" or entry is None or client_id != CLIENT_ID:
        raise HTTPException(400, "invalid_grant")
    digest = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
    if digest != entry["challenge"] or redirect_uri != entry["redirect_uri"]:
        raise HTTPException(400, "invalid_grant")
    now = int(time.time())
    sub = entry["sub"]
    common = {"iss": ISSUER, "sub": sub, "iat": now, "exp": now + 3600}
    access = _sign({**common, "client_id": CLIENT_ID, "token_use": "access", "custom:tenant_id": TENANT, "scope": "openid email"})
    id_token = _sign({**common, "aud": CLIENT_ID, "token_use": "id", "email": f"{sub}@example.test", "nonce": entry["nonce"]})
    return {"access_token": access, "id_token": id_token, "token_type": "Bearer", "expires_in": 3600}


@app.get("/logout")
def logout(logout_uri: str = "/", client_id: str = ""):
    return RedirectResponse(logout_uri, status_code=302)
