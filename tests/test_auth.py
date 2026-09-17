"""Accounts: the gate, the invite list, profiles, and both ways to set a password.

Supabase is stubbed throughout. What is being tested is Vesta's half of the exchange:
which requests it makes, what it does with the answers, and what it refuses.
"""
import base64
import hashlib
import hmac
import json
import time

import pytest

import auth as vesta_auth
import app as vesta_app


# ---------------------------------------------------------------------------
# A stand-in for Supabase
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.content = b"x"

    def json(self):
        return self._payload


class FakeSupabase:
    """Records every call, and answers the way Supabase would."""

    def __init__(self):
        self.users = {}            # email -> password
        self.calls = []
        self.tokens = {}           # token -> email

    def token_for(self, email):
        tok = f"token-for-{email}"
        self.tokens[tok] = email
        return tok

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json))
        payload = json or {}
        email = (payload.get("email") or "").strip()
        if url.endswith("token?grant_type=password"):
            if self.users.get(email) != payload.get("password"):
                return FakeResponse(400, {"msg": "Invalid login credentials"})
            return FakeResponse(200, {"access_token": self.token_for(email)})
        if url.endswith("/signup"):
            if email in self.users:
                return FakeResponse(400, {"msg": "User already registered"})
            self.users[email] = payload.get("password")
            return FakeResponse(200, {"access_token": self.token_for(email)})
        if url.endswith("/recover"):
            return FakeResponse(200, {})
        return FakeResponse(404, {"msg": "no such route"})

    def get(self, url, headers=None, timeout=None):
        self.calls.append(("GET", url, None))
        token = (headers or {}).get("Authorization", "").replace("Bearer ", "")
        email = self.tokens.get(token)
        if not email:
            return FakeResponse(401, {"msg": "bad token"})
        return FakeResponse(200, {"id": f"uuid-{email}", "email": email})

    def put(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("PUT", url, json))
        token = (headers or {}).get("Authorization", "").replace("Bearer ", "")
        email = self.tokens.get(token)
        if not email:
            return FakeResponse(401, {"msg": "bad token"})
        if json and "password" in json:
            self.users[email] = json["password"]
        return FakeResponse(200, {"id": f"uuid-{email}", "email": email})


@pytest.fixture
def supa(monkeypatch):
    fake = FakeSupabase()
    import httpx
    monkeypatch.setattr(httpx, "post", fake.post)
    monkeypatch.setattr(httpx, "get", fake.get)
    monkeypatch.setattr(httpx, "put", fake.put)
    return fake


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("INVITE_EMAILS", raising=False)
    vesta_app.app.config["TESTING"] = True
    with vesta_app.app.test_client() as c:
        yield c


def sign_in(client, supa, email="saif@example.com", password="hunter2222"):
    supa.users[email] = password
    return client.post("/api/auth/login", json={"email": email, "password": password})


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
def test_api_refuses_without_a_session(client):
    r = client.get("/api/state")
    assert r.status_code == 401
    assert r.get_json()["signedOut"] is True


def test_the_page_itself_still_loads_signed_out(client):
    assert client.get("/").status_code == 200


def test_me_reports_accounts_but_no_user(client):
    body = client.get("/api/auth/me").get_json()
    assert body["accounts"] is True
    assert body["user"] is None
    assert body["inviteOnly"] is False


# ---------------------------------------------------------------------------
# Sign in, sign out
# ---------------------------------------------------------------------------
def test_login_then_api_works_then_logout_closes_it(client, supa):
    assert sign_in(client, supa).status_code == 200
    assert client.get("/api/state").status_code == 200
    assert client.post("/api/auth/logout", json={}).status_code == 200
    assert client.get("/api/state").status_code == 401


def test_login_with_a_wrong_password_is_refused(client, supa):
    supa.users["saif@example.com"] = "hunter2222"
    r = client.post("/api/auth/login",
                    json={"email": "saif@example.com", "password": "wrong"})
    assert r.status_code == 401
    assert client.get("/api/state").status_code == 401


def test_signup_creates_a_session(client, supa):
    r = client.post("/api/auth/signup",
                    json={"email": "new@example.com", "password": "longenough1"})
    assert r.status_code == 200
    assert r.get_json()["user"]["email"] == "new@example.com"
    assert client.get("/api/state").status_code == 200


def test_signup_rejects_a_short_password(client, supa):
    r = client.post("/api/auth/signup",
                    json={"email": "new@example.com", "password": "short"})
    assert r.status_code == 400


def test_reset_gives_the_same_answer_for_any_email(client, supa):
    known = client.post("/api/auth/reset", json={"email": "saif@example.com"}).get_json()
    unknown = client.post("/api/auth/reset", json={"email": "nobody@example.com"}).get_json()
    assert known == unknown
    assert known["ok"] is True


# ---------------------------------------------------------------------------
# The invite list
# ---------------------------------------------------------------------------
def test_unset_invite_list_lets_anyone_in(client, supa, monkeypatch):
    monkeypatch.delenv("INVITE_EMAILS", raising=False)
    assert sign_in(client, supa, "stranger@example.com").status_code == 200


def test_invite_list_blocks_an_uninvited_email(client, supa, monkeypatch):
    monkeypatch.setenv("INVITE_EMAILS", "saif@example.com, joe@example.com")
    r = sign_in(client, supa, "stranger@example.com")
    assert r.status_code == 403
    assert "not been invited" in r.get_json()["error"]
    assert client.get("/api/state").status_code == 401


def test_invite_list_admits_an_invited_email_ignoring_case_and_space(client, supa, monkeypatch):
    monkeypatch.setenv("INVITE_EMAILS", "  Saif@Example.com , joe@example.com ")
    assert sign_in(client, supa, "saif@example.com").status_code == 200


def test_google_sign_in_is_gated_by_the_same_list(client, supa, monkeypatch):
    """The whole reason the check lives in _session_from_token and not in signup."""
    monkeypatch.setenv("INVITE_EMAILS", "saif@example.com")
    token = supa.token_for("stranger@example.com")
    r = client.post("/api/auth/session", json={"accessToken": token})
    assert r.status_code == 403
    assert client.get("/api/state").status_code == 401


@pytest.mark.parametrize("raw", [
    "saif@example.com",
    '"saif@example.com"',
    "'saif@example.com'",
    "saif@example.com;joe@example.com",
    "saif@example.com joe@example.com",
    " SAIF@Example.com \n joe@example.com ",
    "saif@example.com,joe@example.com,",
])
def test_the_list_survives_however_it_was_typed(client, supa, monkeypatch, raw):
    """This value is pasted into a dashboard by hand.

    Quotes and semicolons used to produce a list matching nothing, which does not fail
    loudly -- it locks the owner out of his own app at his next sign-in.
    """
    monkeypatch.setenv("INVITE_EMAILS", raw)
    assert sign_in(client, supa, "saif@example.com").status_code == 200


@pytest.mark.parametrize("raw", ['"saif@example.com"', "saif@example.com;joe@example.com"])
def test_a_stranger_is_still_refused_however_it_was_typed(client, supa, monkeypatch, raw):
    monkeypatch.setenv("INVITE_EMAILS", raw)
    assert sign_in(client, supa, "stranger@example.com").status_code == 403


def test_a_blank_or_whitespace_list_means_open(client, supa, monkeypatch):
    """Not a lockout: an empty variable has to behave exactly like an unset one."""
    monkeypatch.setenv("INVITE_EMAILS", "   ,  , ")
    assert sign_in(client, supa, "anyone@example.com").status_code == 200


def test_an_uninvited_signup_never_reaches_supabase(client, supa, monkeypatch):
    """Refused before anything is created, not after.

    The gate in _session_from_token runs only once a token comes back. With email
    confirmation on there is no token, so an uninvited stranger used to be told to check
    their inbox, having had a real Supabase account made for them.
    """
    monkeypatch.setenv("INVITE_EMAILS", "saif@example.com")
    r = client.post("/api/auth/signup",
                    json={"email": "stranger@example.com", "password": "longenough1"})
    assert r.status_code == 403
    assert supa.calls == []                      # Supabase was never called
    assert "stranger@example.com" not in supa.users


def test_an_invited_signup_still_goes_through(client, supa, monkeypatch):
    monkeypatch.setenv("INVITE_EMAILS", "saif@example.com")
    r = client.post("/api/auth/signup",
                    json={"email": "saif@example.com", "password": "longenough1"})
    assert r.status_code == 200
    assert "saif@example.com" in supa.users


def test_me_says_invite_only_when_a_list_is_set(client, monkeypatch):
    monkeypatch.setenv("INVITE_EMAILS", "saif@example.com")
    assert client.get("/api/auth/me").get_json()["inviteOnly"] is True


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------
def test_profile_needs_a_session(client):
    assert client.get("/api/auth/profile").status_code == 401


def test_profile_roundtrips_a_display_name(client, supa):
    sign_in(client, supa)
    assert client.get("/api/auth/profile").get_json()["displayName"] == ""
    saved = client.post("/api/auth/profile", json={"displayName": "Saif"}).get_json()
    assert saved["displayName"] == "Saif"
    assert client.get("/api/auth/profile").get_json()["displayName"] == "Saif"
    assert client.get("/api/auth/profile").get_json()["email"] == "saif@example.com"


def test_profile_rejects_an_absurd_name(client, supa):
    sign_in(client, supa)
    r = client.post("/api/auth/profile", json={"displayName": "x" * 200})
    assert r.status_code == 400


def test_profile_name_can_be_cleared(client, supa):
    sign_in(client, supa)
    client.post("/api/auth/profile", json={"displayName": "Saif"})
    assert client.post("/api/auth/profile", json={"displayName": "  "}).get_json()["displayName"] == ""


# ---------------------------------------------------------------------------
# Changing a password while signed in
# ---------------------------------------------------------------------------
def test_change_password_needs_the_current_one(client, supa):
    sign_in(client, supa)
    r = client.post("/api/auth/password", json={"newPassword": "brandnew123"})
    assert r.status_code == 400
    assert "current password" in r.get_json()["error"]


def test_change_password_refuses_a_wrong_current_password(client, supa):
    sign_in(client, supa)
    r = client.post("/api/auth/password",
                    json={"currentPassword": "nope", "newPassword": "brandnew123"})
    assert r.status_code == 401
    assert supa.users["saif@example.com"] == "hunter2222"    # unchanged


def test_change_password_refuses_a_short_new_password(client, supa):
    sign_in(client, supa)
    r = client.post("/api/auth/password",
                    json={"currentPassword": "hunter2222", "newPassword": "short"})
    assert r.status_code == 400


def test_change_password_works_and_actually_changes_it(client, supa):
    sign_in(client, supa)
    r = client.post("/api/auth/password",
                    json={"currentPassword": "hunter2222", "newPassword": "brandnew123"})
    assert r.status_code == 200
    assert supa.users["saif@example.com"] == "brandnew123"
    assert any(m == "PUT" for m, _, _ in supa.calls)


def test_change_password_needs_a_session(client, supa):
    r = client.post("/api/auth/password",
                    json={"currentPassword": "x", "newPassword": "brandnew123"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Setting a password from a reset email
# ---------------------------------------------------------------------------
def test_recovery_token_sets_the_password_and_signs_in(client, supa):
    supa.users["saif@example.com"] = "forgotten"
    token = supa.token_for("saif@example.com")
    r = client.post("/api/auth/password",
                    json={"recoveryToken": token, "newPassword": "brandnew123"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["signedIn"] is True
    assert body["user"]["email"] == "saif@example.com"
    assert supa.users["saif@example.com"] == "brandnew123"
    assert client.get("/api/state").status_code == 200


def test_a_junk_recovery_token_changes_nothing(client, supa):
    supa.users["saif@example.com"] = "forgotten"
    r = client.post("/api/auth/password",
                    json={"recoveryToken": "not-a-token", "newPassword": "brandnew123"})
    assert r.status_code >= 400
    assert supa.users["saif@example.com"] == "forgotten"
    assert client.get("/api/state").status_code == 401


def test_recovery_still_honours_the_invite_list(client, supa, monkeypatch):
    monkeypatch.setenv("INVITE_EMAILS", "saif@example.com")
    supa.users["stranger@example.com"] = "forgotten"
    token = supa.token_for("stranger@example.com")
    r = client.post("/api/auth/password",
                    json={"recoveryToken": token, "newPassword": "brandnew123"})
    assert r.status_code == 403
    assert client.get("/api/state").status_code == 401


def test_password_route_is_reachable_signed_out(client):
    """It has to be: the whole point is that the person cannot sign in."""
    assert "/api/auth/password" in vesta_auth.OPEN_PATHS


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------
def _jwt(claims, secret, alg="HS256"):
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    head, body = seg({"alg": alg, "typ": "JWT"}), seg(claims)
    sig = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{base64.urlsafe_b64encode(sig).rstrip(b'=').decode()}"


def test_verify_jwt_accepts_a_genuine_token():
    tok = _jwt({"sub": "abc", "email": "a@b.c", "exp": time.time() + 60}, "s3cret")
    assert vesta_auth.verify_jwt(tok, "s3cret")["sub"] == "abc"


def test_verify_jwt_rejects_a_forged_signature():
    tok = _jwt({"sub": "abc", "exp": time.time() + 60}, "wrong-secret")
    with pytest.raises(vesta_auth.AuthError):
        vesta_auth.verify_jwt(tok, "s3cret")


def test_verify_jwt_rejects_alg_none():
    tok = _jwt({"sub": "abc", "exp": time.time() + 60}, "s3cret", alg="none")
    with pytest.raises(vesta_auth.AuthError):
        vesta_auth.verify_jwt(tok, "s3cret")


def test_verify_jwt_rejects_an_expired_token():
    tok = _jwt({"sub": "abc", "exp": time.time() - 1}, "s3cret")
    with pytest.raises(vesta_auth.AuthError):
        vesta_auth.verify_jwt(tok, "s3cret")
