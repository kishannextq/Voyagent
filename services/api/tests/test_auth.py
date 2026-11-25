"""Authentication (Cognito-style RS256 access tokens) and plan ownership (SECURITY.md F2).

Tokens are signed with locally generated RSA keys and served by a static JWKS, so the full
verification path (signature, issuer, expiry, token_use, client_id) runs without AWS.
"""

import asyncio
import json
import os
import time
import uuid

os.environ["MOCK_MODE"] = "true"
os.environ["MOCK_DELAY"] = "0"

import jwt  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import orchestrator  # noqa: E402
import server  # noqa: E402
import threads  # noqa: E402

ISSUER = "https://cognito-idp.test.example/pool_test"
CLIENT = "web-client-id"


def _keypair(kid: str):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    public.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return key, public


SIGNING_KEY, SIGNING_JWK = _keypair("k1")
OTHER_KEY, _ = _keypair("k1")  # same kid, different key: signature must fail


class StaticJWKS:
    """Stands in for PyJWKClient (no network)."""

    def __init__(self, *_, **__):
        self._keys = {SIGNING_JWK["kid"]: jwt.PyJWK(SIGNING_JWK)}

    def get_signing_key_from_jwt(self, token):
        kid = jwt.get_unverified_header(token).get("kid")
        if kid not in self._keys:
            raise jwt.PyJWKClientError("Unable to find a signing key")
        return self._keys[kid]


def token(sub="alice", tenant="acme", groups=None, key=SIGNING_KEY, kid="k1", **overrides) -> str:
    now = int(time.time())
    claims = {
        "sub": sub,
        "iss": ISSUER,
        "client_id": CLIENT,
        "token_use": "access",
        "iat": now,
        "exp": now + 600,
        "custom:tenant_id": tenant,
        "cognito:groups": groups or [],
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": kid})


def bearer(t: str) -> dict:
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture
def cognito_env(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "cognito")
    monkeypatch.setenv("COGNITO_ISSUER", ISSUER)
    monkeypatch.setenv("COGNITO_CLIENT_IDS", CLIENT)
    monkeypatch.setattr(auth.jwt, "PyJWKClient", StaticJWKS)


@pytest.fixture
def client(cognito_env):
    with TestClient(server.api) as c:
        yield c


PLAN = {"query": "3 days in Rome"}


def _plan(client, t) -> str:
    r = client.post("/api/v1/plan", json=PLAN, headers=bearer(t))
    assert r.status_code == 200, r.text
    return r.json()["thread_id"]


# ---------------- authentication ----------------


def test_protected_routes_require_a_token(client):
    for method, path in [("post", "/api/v1/plan"), ("get", "/api/v1/plans"), ("get", "/api/v1/plan/abcdefgh12")]:
        r = getattr(client, method)(path, json=PLAN) if method == "post" else client.get(path)
        assert r.status_code == 401, path
        assert r.headers["www-authenticate"] == "Bearer"
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/agents").status_code == 200


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(lambda: token(exp=int(time.time()) - 120), id="expired"),
        pytest.param(lambda: token(iss="https://evil.example/pool"), id="wrong-issuer"),
        pytest.param(lambda: token(client_id="other-app"), id="wrong-client"),
        pytest.param(lambda: token(token_use="id"), id="id-token"),
        pytest.param(lambda: token(key=OTHER_KEY), id="bad-signature"),
        pytest.param(lambda: token(kid="unknown"), id="unknown-kid"),
        pytest.param(lambda: token(sub=None), id="no-sub"),
        pytest.param(
            lambda: jwt.encode({"sub": "x", "iss": ISSUER, "exp": int(time.time()) + 60}, "s" * 32, algorithm="HS256"),
            id="hs256",
        ),
        pytest.param(lambda: "not.a.jwt", id="garbage"),
    ],
)
def test_invalid_tokens_rejected(client, bad):
    r = client.post("/api/v1/plan", json=PLAN, headers=bearer(bad()))
    assert r.status_code == 401
    assert r.json() == {"detail": "Not authenticated"}  # no hint why


def test_alg_none_rejected(client):
    unsigned = jwt.encode(
        {"sub": "x", "iss": ISSUER, "client_id": CLIENT, "token_use": "access"}, None, algorithm="none"
    )
    assert client.post("/api/v1/plan", json=PLAN, headers=bearer(unsigned)).status_code == 401


def test_identity_comes_from_token_not_body(client):
    tid = _plan(client, token(sub="alice"))
    snap = asyncio.run(server.orch.graph.aget_state(orchestrator.config(tid)))
    assert snap.values["user_id"] == "alice"
    r = client.post("/api/v1/plan", json={**PLAN, "user_id": "mallory"}, headers=bearer(token(sub="alice")))
    snap = asyncio.run(server.orch.graph.aget_state(orchestrator.config(r.json()["thread_id"])))
    assert snap.values["user_id"] == "alice"


# ---------------- ownership ----------------


def test_only_owner_or_tenant_admin_can_read_or_resume(client):
    tid = _plan(client, token(sub="alice", tenant="acme"))
    assert client.get(f"/api/v1/plan/{tid}", headers=bearer(token(sub="alice"))).status_code == 200

    other_user = bearer(token(sub="bob", tenant="acme"))
    other_tenant_admin = bearer(token(sub="eve", tenant="globex", groups=["admin"]))
    for h in (other_user, other_tenant_admin):
        assert client.get(f"/api/v1/plan/{tid}", headers=h).status_code == 404
        r = client.post(f"/api/v1/plan/{tid}/approve", json={"approved": True}, headers=h)
        assert r.status_code == 404
        r = client.post(f"/api/v1/plan/{tid}/approve/stream", json={"approved": True}, headers=h)
        assert r.status_code == 404

    same_tenant_admin = bearer(token(sub="carol", tenant="acme", groups=["admin"]))
    assert client.get(f"/api/v1/plan/{tid}", headers=same_tenant_admin).status_code == 200
    # The legacy alias enforces the same checks.
    assert client.get(f"/api/plan/{tid}", headers=other_user).status_code == 404


def test_unknown_and_foreign_plans_look_the_same(client):
    tid = _plan(client, token(sub="alice"))
    h = bearer(token(sub="bob"))
    foreign = client.get(f"/api/v1/plan/{tid}", headers=h)
    unknown = client.get("/api/v1/plan/doesnotexist123", headers=h)
    assert foreign.status_code == unknown.status_code == 404


def test_cannot_claim_someone_elses_thread_id(client):
    tid = _plan(client, token(sub="alice"))
    r = client.post("/api/v1/plan", json={**PLAN, "thread_id": tid}, headers=bearer(token(sub="bob")))
    assert r.status_code == 409


def test_list_plans_own_only_admin_can_list_tenant(client):
    a1 = _plan(client, token(sub="alice"))
    a2 = _plan(client, token(sub="alice"))
    b1 = _plan(client, token(sub="bob"))
    _plan(client, token(sub="zed", tenant="globex"))

    mine = client.get("/api/v1/plans", headers=bearer(token(sub="alice"))).json()["items"]
    assert [p["thread_id"] for p in mine] == [a2, a1]

    assert client.get("/api/v1/plans?all_users=true", headers=bearer(token(sub="alice"))).status_code == 403
    admin = client.get("/api/v1/plans?all_users=true", headers=bearer(token(sub="carol", groups=["admin"]))).json()
    ids = {p["thread_id"] for p in admin["items"]}
    assert {a1, a2, b1} <= ids and all(not p["owner"] for p in admin["items"])  # nothing from globex
    assert len(ids) == 3

    page = client.get("/api/v1/plans?limit=1", headers=bearer(token(sub="alice"))).json()
    assert [p["thread_id"] for p in page["items"]] == [a2] and page["next_before"]
    nxt = client.get(
        "/api/v1/plans", params={"limit": 1, "before": page["next_before"]}, headers=bearer(token(sub="alice"))
    )
    assert [p["thread_id"] for p in nxt.json()["items"]] == [a1]


def test_rate_limits_key_on_user(cognito_env, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_RUNS_PER_MINUTE", "1")
    with TestClient(server.api) as c:
        assert c.post("/api/v1/plan", json=PLAN, headers=bearer(token(sub="alice"))).status_code == 200
        assert c.post("/api/v1/plan", json=PLAN, headers=bearer(token(sub="bob"))).status_code == 200  # same IP
        assert c.post("/api/v1/plan", json=PLAN, headers=bearer(token(sub="alice"))).status_code == 429


# ---------------- configuration policy ----------------


@pytest.mark.parametrize("env", ["staging", "production"])
def test_auth_cannot_be_disabled_outside_local(monkeypatch, env):
    monkeypatch.setenv("APP_ENV", env)
    monkeypatch.setenv("AUTH_MODE", "disabled")
    with pytest.raises(RuntimeError, match="not allowed"):
        auth.AuthConfig.from_env()
    monkeypatch.delenv("AUTH_MODE")
    with pytest.raises(RuntimeError, match="COGNITO"):  # default is cognito, which needs config
        auth.AuthConfig.from_env()


def test_cognito_issuer_derived_from_region_and_pool(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "cognito")
    monkeypatch.delenv("COGNITO_ISSUER", raising=False)
    monkeypatch.setenv("COGNITO_REGION", "eu-west-1")
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "eu-west-1_AbC")
    monkeypatch.setenv("COGNITO_CLIENT_IDS", "a, b")
    cfg = auth.AuthConfig.from_env()
    assert cfg.issuer == "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_AbC"
    assert cfg.jwks_url.endswith("/eu-west-1_AbC/.well-known/jwks.json")
    assert cfg.client_ids == {"a", "b"}


def test_disabled_mode_uses_fixed_local_user(monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "disabled")
    with TestClient(server.api) as c:
        tid = c.post("/api/v1/plan", json=PLAN).json()["thread_id"]
        assert c.get("/api/v1/plans").json()["items"][0]["thread_id"] == tid


# ---------------- thread store ----------------


def test_memory_thread_store():
    async def go():
        s = threads.MemoryThreadStore(max_entries=2)
        await s.create("t1aaaaaa", "acme", "alice")
        with pytest.raises(threads.ThreadExists):
            await s.create("t1aaaaaa", "acme", "bob")
        await s.create("t2aaaaaa", "acme", "alice")
        await s.create("t3aaaaaa", "acme", "alice")  # evicts the oldest (bounded)
        assert await s.get("t1aaaaaa") is None
        assert [r.thread_id for r in await s.list("acme", "alice", 10)] == ["t3aaaaaa", "t2aaaaaa"]
        assert await s.list("globex", None, 10) == []

    asyncio.run(go())


def test_thread_ids_generated_are_unique(client):
    t = token(sub="alice")
    ids = {_plan(client, t) for _ in range(5)}
    assert len(ids) == 5 and all(len(i) == 32 for i in ids)
    assert uuid.UUID(next(iter(ids)))
