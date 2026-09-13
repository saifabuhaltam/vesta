"""Accounts, for the deployed app only.

Vesta ran as one person's private tool for a long time, and locally it still does: with
no Supabase configured there is no login, no session and no user, exactly as before.
Authentication switches itself on only when the three environment variables below are
present, which is the case on Railway and nowhere else.

**Supabase issues the tokens; Flask holds the session.** The browser never talks to
Supabase and never sees the anon key: it posts an email and password to Flask, Flask
exchanges them with Supabase, verifies the returned token, and sets its own signed
session cookie. Two reasons for that shape:

* a cookie is sent by `<img src>`, `<iframe>` and a download link, and an Authorization
  header is not. Those are exactly how the app shows a stored file, so a Bearer-token
  design would have needed a second, token-in-the-query-string path for reads;
* nothing about the page has to change. The existing fetch helpers already send
  cookies, so the whole frontend keeps working untouched.

The token signature is checked locally with `hmac`, not by asking Supabase on every
request. Asking would be a round trip per request for a value that cannot change
mid-token, and the shared secret makes local verification exact.
"""
import base64
import hashlib
import hmac
import json
import os
import time

from flask import Blueprint, g, jsonify, request, session

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY") or ""
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET") or ""

# How long a sign-in lasts. Supabase's own access token expires in about an hour, but
# it is used once, at sign-in, and never again: from then on identity is carried by
# Flask's signed cookie, so this is the number that actually governs. Long enough not
# to interrupt someone between classes, short enough that a removed account does not
# keep working forever.
SESSION_SECONDS = 60 * 60 * 24 * 30

# Paths that must work before anyone is signed in.
OPEN_PATHS = {"/api/auth/login", "/api/auth/signup", "/api/auth/reset",
              "/api/auth/logout", "/api/auth/me", "/health"}

bp = Blueprint("auth", __name__)


def enabled():
    """True only where accounts are configured, which is the deployed app.

    The JWT secret is deliberately not required. See `identify`.
    """
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY)


class AuthError(Exception):
    def __init__(self, message, status=401):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------
# Token verification, with no dependency
# ---------------------------------------------------------------------------
def _b64url(segment):
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def verify_jwt(token, secret=None):
    """Return the claims of a Supabase access token, or raise.

    Deliberately strict about the algorithm: accepting whatever the token's own header
    asks for is how `alg: none` attacks work.
    """
    secret = secret or SUPABASE_JWT_SECRET
    parts = (token or "").split(".")
    if len(parts) != 3:
        raise AuthError("That session token is malformed.")
    head_b64, body_b64, sig_b64 = parts
    try:
        head = json.loads(_b64url(head_b64))
        claims = json.loads(_b64url(body_b64))
        signature = _b64url(sig_b64)
    except Exception:
        raise AuthError("That session token could not be read.")

    if head.get("alg") != "HS256":
        raise AuthError("That session token uses an algorithm Vesta does not accept.")
    expected = hmac.new(secret.encode("utf-8"),
                        f"{head_b64}.{body_b64}".encode("utf-8"),
                        hashlib.sha256).digest()
    if not hmac.compare_digest(expected, signature):
        raise AuthError("That session token is not genuine.")
    if claims.get("exp") and float(claims["exp"]) < time.time():
        raise AuthError("Your session has expired. Sign in again.")
    if not claims.get("sub"):
        raise AuthError("That session token carries no account.")
    return claims


# ---------------------------------------------------------------------------
# Talking to Supabase
# ---------------------------------------------------------------------------
def _supabase(path, payload):
    import httpx
    try:
        r = httpx.post(f"{SUPABASE_URL}/auth/v1/{path}",
                       json=payload,
                       headers={"apikey": SUPABASE_ANON_KEY,
                                "Content-Type": "application/json"},
                       timeout=20)
    except Exception as e:
        raise AuthError(f"Could not reach the sign-in service: {e}", 502)
    body = {}
    try:
        body = r.json()
    except Exception:
        pass
    if r.status_code >= 400:
        # Supabase's own wording is the most useful thing to show here
        msg = (body.get("msg") or body.get("error_description")
               or body.get("message") or body.get("error") or "Sign-in failed.")
        raise AuthError(msg, 401 if r.status_code in (400, 401, 403) else 502)
    return body


def ensure_account_row(user_id, email):
    """The schema hangs every row off auth.users, so the account must exist there.

    Written as the connecting role rather than the signed-in one, because a student
    cannot be allowed to insert arbitrary accounts.
    """
    import db
    if not db.DATABASE_URL:
        return
    conn = db.get_db(user_id=None)
    try:
        conn.as_owner()
        conn.execute("insert into auth.users (id, email) values (?, ?) "
                     "on conflict (id) do update set email = excluded.email",
                     (user_id, email or ""))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def identify(access_token):
    """Who a Supabase access token belongs to, as (user_id, email).

    Asked of Supabase rather than worked out from the signature, because Supabase has
    been moving projects off a single shared HS256 secret onto asymmetric ES256 signing
    keys. Verifying ES256 here would mean an elliptic-curve dependency and fetching a
    JWKS, all to avoid one HTTP call that happens once per sign-in and never again:
    after this, identity is carried by Flask's own signed session cookie.

    Where a shared secret *is* configured, the signature is checked locally first. That
    is free, needs no network, and rejects a forged token outright.
    """
    if SUPABASE_JWT_SECRET:
        try:
            claims = verify_jwt(access_token)
            return claims["sub"], claims.get("email") or ""
        except AuthError:
            # A legacy secret that no longer matches how this project signs tokens is a
            # reason to ask, not a reason to refuse a genuine sign-in.
            pass

    import httpx
    try:
        r = httpx.get(f"{SUPABASE_URL}/auth/v1/user",
                      headers={"Authorization": f"Bearer {access_token}",
                               "apikey": SUPABASE_ANON_KEY},
                      timeout=20)
    except Exception as e:
        raise AuthError(f"Could not reach the sign-in service: {e}", 502)
    if r.status_code >= 400:
        raise AuthError("That sign-in could not be confirmed.")
    user = r.json() or {}
    if not user.get("id"):
        raise AuthError("That sign-in carried no account.")
    return user["id"], user.get("email") or ""


def _session_from_token(access_token):
    user_id, email = identify(access_token)
    ensure_account_row(user_id, email)
    session.permanent = True
    session["user_id"] = user_id
    session["email"] = email
    session["exp"] = time.time() + SESSION_SECONDS
    return {"id": user_id, "email": email}


@bp.route("/api/auth/login", methods=["POST"])
def login():
    if not enabled():
        return jsonify({"error": "This copy of Vesta has no accounts."}), 400
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    if not email or not password:
        return jsonify({"error": "Give your email and password."}), 400
    try:
        body = _supabase("token?grant_type=password", {"email": email, "password": password})
        user = _session_from_token(body.get("access_token") or "")
    except AuthError as e:
        return jsonify({"error": e.message}), e.status
    return jsonify({"user": user})


@bp.route("/api/auth/signup", methods=["POST"])
def signup():
    """Signup is gated by Supabase's own allowlist, so a refusal here is expected and
    the message Supabase gives is the one worth showing."""
    if not enabled():
        return jsonify({"error": "This copy of Vesta has no accounts."}), 400
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    if not email or len(password) < 8:
        return jsonify({"error": "Give your email and a password of at least 8 characters."}), 400
    try:
        body = _supabase("signup", {"email": email, "password": password})
    except AuthError as e:
        return jsonify({"error": e.message}), e.status
    # With email confirmation on, there is no token yet and that is not an error.
    if body.get("access_token"):
        try:
            return jsonify({"user": _session_from_token(body["access_token"])})
        except AuthError as e:
            return jsonify({"error": e.message}), e.status
    return jsonify({"pending": True,
                    "message": "Check your email for a confirmation link, then sign in."})


@bp.route("/api/auth/reset", methods=["POST"])
def reset():
    if not enabled():
        return jsonify({"error": "This copy of Vesta has no accounts."}), 400
    email = ((request.get_json(force=True) or {}).get("email") or "").strip()
    if not email:
        return jsonify({"error": "Give your email."}), 400
    try:
        _supabase("recover", {"email": email})
    except AuthError as e:
        return jsonify({"error": e.message}), e.status
    # Always the same answer, so this cannot be used to find out who has an account.
    return jsonify({"ok": True, "message": "If that email has an account, a reset link is on its way."})


@bp.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@bp.route("/api/auth/me")
def me():
    if not enabled():
        return jsonify({"accounts": False, "user": None})
    uid = session.get("user_id")
    if not uid:
        return jsonify({"accounts": True, "user": None})
    return jsonify({"accounts": True, "user": {"id": uid, "email": session.get("email") or ""}})


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
def install(app):
    """Wire accounts into the app. A no-op where they are not configured."""
    app.register_blueprint(bp)
    secret = os.environ.get("SECRET_KEY")
    if enabled() and not secret:
        raise RuntimeError("SECRET_KEY must be set when Supabase accounts are configured: "
                           "it signs the session cookie.")
    app.secret_key = secret or "local-only-no-accounts"
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(os.environ.get("HTTPS_ONLY", "1") == "1" and enabled()),
        PERMANENT_SESSION_LIFETIME=SESSION_SECONDS,
    )

    @app.before_request
    def _require_account():
        g.user_id = None
        if not enabled():
            return None                    # local single-user: nothing to check
        path = request.path
        if path in OPEN_PATHS or request.method == "OPTIONS":
            return None
        # the page itself loads so it can show a sign-in screen; its data calls do not
        if path == "/" or not path.startswith("/api/"):
            return None
        uid = session.get("user_id")
        exp = session.get("exp")
        if exp and float(exp) < time.time():
            session.clear()
            uid = None
        if not uid:
            return jsonify({"error": "Sign in to use Vesta.", "signedOut": True}), 401
        g.user_id = uid
        return None
