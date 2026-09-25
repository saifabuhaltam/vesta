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
import functools
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.parse

from flask import Blueprint, g, jsonify, request, session

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
SUPABASE_ANON_KEY = (os.environ.get("SUPABASE_ANON_KEY") or "").strip()
SUPABASE_JWT_SECRET = (os.environ.get("SUPABASE_JWT_SECRET") or "").strip()

# How long a sign-in lasts. Supabase's own access token expires in about an hour, but
# it is used once, at sign-in, and never again: from then on identity is carried by
# Flask's signed cookie, so this is the number that actually governs. Long enough not
# to interrupt someone between classes, short enough that a removed account does not
# keep working forever.
SESSION_SECONDS = 60 * 60 * 24 * 30

# Paths that must work before anyone is signed in.
# Google's push notifications arrive from Google, not from a signed-in browser, so
# this one cannot be gated. It is written to trust nothing in the request but the
# channel id, which it looks up before doing anything at all.
OPEN_PATHS = {"/api/calendar/google/webhook",
              "/api/auth/login", "/api/auth/signup", "/api/auth/reset",
              "/api/auth/logout", "/api/auth/me", "/api/auth/google/start",
              "/api/auth/session", "/api/auth/password", "/health"}

bp = Blueprint("auth", __name__)


def enabled():
    """True only where accounts are configured, which is the deployed app.

    The JWT secret is deliberately not required. See `identify`.
    """
    return bool(SUPABASE_URL and SUPABASE_ANON_KEY)


def allowlist():
    """Emails permitted to reach an account, from `INVITE_EMAILS`.

    An environment variable rather than a table, because adding a friend is then a
    Railway dashboard edit rather than a psql session, and this list is expected to
    hold single figures.

    Unset means open, deliberately. The alternative locks Saif out of his own app the
    first time the variable is missing on a deploy, which is a far worse failure than
    the one it prevents. `/api/auth/me` reports which mode is in force so the sign-in
    screen can stop claiming to be invite-only when it is not.
    """
    raw = (os.environ.get("INVITE_EMAILS") or "").strip()
    # Forgiving on purpose. This value is typed into a dashboard by hand, and the two
    # ways it goes wrong -- wrapping it in quotes, or separating with semicolons --
    # both used to produce a list that matched nothing, which locks the owner out of
    # his own app at the next sign-in rather than failing where anyone would see it.
    for sep in (";", "\n", "\r", " "):
        raw = raw.replace(sep, ",")
    out = set()
    for part in raw.split(","):
        cleaned = part.strip().strip('"').strip("'").strip()
        if cleaned:
            out.add(cleaned.lower())
    return out


def invited(email):
    allow = allowlist()
    return True if not allow else (email or "").strip().lower() in allow


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
# Rate limiting
# ---------------------------------------------------------------------------
# Every sign-in is Flask calling Supabase, so Supabase's own per-address limit sees
# one address, Railway's, for everybody. Without a limit here, one person hammering
# the login form would use up the allowance and lock out every account at once. Ten
# tries in five minutes is far more than anyone typing a password needs.
#
# In memory, which is right for one gunicorn worker: it resets on a deploy, and a
# second worker would need a shared store. The address comes from Cloudflare's header
# because every request arrives through Cloudflare. A request sent straight to
# Railway could forge it and so get around this limit, which leaves things no worse
# than before it existed.
AUTH_WINDOW_SECONDS = 5 * 60
AUTH_MAX_TRIES = 10
_tries = {}
_tries_lock = threading.Lock()


def client_address():
    return (request.headers.get("CF-Connecting-IP")
            or (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            or request.remote_addr or "")


def _wait_seconds(key):
    """0 if this attempt may go ahead (and count it), else seconds until one may."""
    now = time.time()
    with _tries_lock:
        recent = [t for t in _tries.get(key, ()) if t > now - AUTH_WINDOW_SECONDS]
        if len(recent) >= AUTH_MAX_TRIES:
            _tries[key] = recent
            return int(recent[0] + AUTH_WINDOW_SECONDS - now) + 1
        recent.append(now)
        _tries[key] = recent
        if len(_tries) > 10000:           # forget addresses that have gone quiet
            for k in [k for k, v in _tries.items() if v[-1] <= now - AUTH_WINDOW_SECONDS]:
                del _tries[k]
    return 0


def rate_limited(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        wait = _wait_seconds(client_address())
        if wait:
            minutes = max(1, round(wait / 60))
            return jsonify({"error": f"Too many attempts. Try again in {minutes} "
                                     f"minute{'s' if minutes != 1 else ''}."}), 429
        return view(*args, **kwargs)
    return wrapper


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
    """Every way in ends here: password sign-in, signup, and Google.

    Which is why the invite check lives here and not in `signup`. Signing in with
    Google never touches the signup route at all, so a check there would leave the
    front door open while looking closed.
    """
    user_id, email = identify(access_token)
    if not invited(email):
        raise AuthError("That email has not been invited to Vesta.", 403)
    ensure_account_row(user_id, email)
    session.permanent = True
    session["user_id"] = user_id
    session["email"] = email
    session["exp"] = time.time() + SESSION_SECONDS
    return {"id": user_id, "email": email}


@bp.route("/api/auth/login", methods=["POST"])
@rate_limited
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
@rate_limited
def signup():
    """Create an account, if that email has been invited."""
    if not enabled():
        return jsonify({"error": "This copy of Vesta has no accounts."}), 400
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    if not email or len(password) < 8:
        return jsonify({"error": "Give your email and a password of at least 8 characters."}), 400
    # Checked here, before Supabase is touched at all. `_session_from_token` checks it
    # too and remains the real backstop, since Google never reaches this route -- but
    # relying on that alone meant an uninvited signup created a genuine Supabase user
    # and only then got refused. With email confirmation on it was worse: no token comes
    # back, so the gate never ran and the stranger was cheerfully told to go and check
    # their inbox, then refused days later at sign-in.
    if not invited(email):
        return jsonify({"error": "That email has not been invited to Vesta."}), 403
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
@rate_limited
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


def public_origin():
    """This app's own address, as the outside world sees it.

    Railway terminates TLS in front of the app, so `request.host_url` says http and a
    redirect built from it would not match what was registered. The proxy header is
    the only thing that knows the real scheme.
    """
    host = request.host or ""
    if host.split(":")[0] in ("localhost", "127.0.0.1"):
        return f"http://{host}"
    proto = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    return f"{'https' if proto in ('', 'https') else proto}://{host}"


@bp.route("/api/auth/google/start")
def google_start():
    """Where to send the browser to sign in with Google.

    Built server side so the page needs no Supabase configuration of its own, and so
    the return address is derived rather than hard-coded.
    """
    if not enabled():
        return jsonify({"error": "This copy of Vesta has no accounts."}), 400
    back = urllib.parse.quote(public_origin() + "/", safe="")
    return jsonify({"url": f"{SUPABASE_URL}/auth/v1/authorize?provider=google&redirect_to={back}"})


@bp.route("/api/auth/session", methods=["POST"])
@rate_limited
def session_from_browser():
    """Turn a token the browser was handed into a Vesta session.

    Signing in with Google returns the token to the *browser*, in the URL fragment,
    which never reaches a server on its own. The page posts it here once; after that
    identity is carried by the session cookie exactly like an email sign-in, so the
    rest of the app cannot tell the two apart.
    """
    if not enabled():
        return jsonify({"error": "This copy of Vesta has no accounts."}), 400
    token = (request.get_json(force=True) or {}).get("accessToken") or ""
    if not token:
        return jsonify({"error": "That sign-in did not come back with a token."}), 400
    try:
        return jsonify({"user": _session_from_token(token)})
    except AuthError as e:
        return jsonify({"error": e.message}), e.status


def _supabase_update_user(access_token, payload):
    """PUT /auth/v1/user as the holder of that token. Supabase's own password write."""
    import httpx
    try:
        r = httpx.put(f"{SUPABASE_URL}/auth/v1/user",
                      json=payload,
                      headers={"apikey": SUPABASE_ANON_KEY,
                               "Authorization": f"Bearer {access_token}",
                               "Content-Type": "application/json"},
                      timeout=20)
    except Exception as e:
        raise AuthError(f"Could not reach the sign-in service: {e}", 502)
    if r.status_code >= 400:
        body = {}
        try:
            body = r.json()
        except Exception:
            pass
        raise AuthError(body.get("msg") or body.get("message") or "That change was refused.",
                        400 if r.status_code < 500 else 502)
    return r.json() if r.content else {}


@bp.route("/api/auth/password", methods=["POST"])
@rate_limited
def change_password():
    """Set a new password, authorised one of two ways.

    Signed in, it costs the current password. Vesta's own session is a cookie that
    lasts 30 days, so a borrowed laptop should not be enough to take an account over;
    and since Flask throws the Supabase token away at sign-in, re-authenticating is
    also the only way to obtain a token allowed to make the change. The safer choice
    and the cheaper one are the same choice here.

    Arriving from a reset email, the recovery token in the link *is* the proof, which
    is what makes this the one route that has to work with no session at all.
    """
    if not enabled():
        return jsonify({"error": "This copy of Vesta has no accounts."}), 400
    data = request.get_json(force=True) or {}
    new_password = data.get("newPassword") or ""
    if len(new_password) < 8:
        return jsonify({"error": "Use a password of at least 8 characters."}), 400

    recovery = (data.get("recoveryToken") or "").strip()
    try:
        if recovery:
            _supabase_update_user(recovery, {"password": new_password})
            # Straight into the app: they have just proved they hold the mailbox, and
            # making them retype the password they set one second ago is pure friction.
            user = _session_from_token(recovery)
            return jsonify({"user": user, "signedIn": True})

        uid, email = session.get("user_id"), session.get("email")
        if not uid:
            return jsonify({"error": "Sign in to change your password.",
                            "signedOut": True}), 401
        current = data.get("currentPassword") or ""
        if not current:
            return jsonify({"error": "Give your current password."}), 400
        body = _supabase("token?grant_type=password",
                         {"email": email, "password": current})
        token = body.get("access_token") or ""
        if not token:
            raise AuthError("That current password was not accepted.")
        _supabase_update_user(token, {"password": new_password})
    except AuthError as e:
        return jsonify({"error": e.message}), e.status
    return jsonify({"ok": True})


DISPLAY_NAME_KEY = "display_name"


@bp.route("/api/auth/profile", methods=["GET", "POST"])
def profile():
    """The display name, kept in `app_settings` beside every other per-user setting.

    Not on `auth.users`: that table is a shadow of Supabase's, holding only what the
    token carries, and giving it columns the token knows nothing about invites the two
    to disagree. `app_settings` is already keyed per user and already covered by row
    level security, so this needs no schema change at all.
    """
    if not enabled():
        return jsonify({"accounts": False, "displayName": ""})
    uid = session.get("user_id")
    if not uid:
        return jsonify({"error": "Sign in to use Vesta.", "signedOut": True}), 401
    import db
    conn = db.get_db()
    try:
        if request.method == "POST":
            name = ((request.get_json(force=True) or {}).get("displayName") or "").strip()
            if len(name) > 80:
                return jsonify({"error": "Keep the name under 80 characters."}), 400
            db.set_setting(conn, DISPLAY_NAME_KEY, name)
        return jsonify({"displayName": db.get_setting(conn, DISPLAY_NAME_KEY),
                        "email": session.get("email") or ""})
    finally:
        conn.close()


@bp.route("/api/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@bp.route("/api/auth/me")
def me():
    if not enabled():
        return jsonify({"accounts": False, "user": None})
    uid = session.get("user_id")
    invite_only = bool(allowlist())
    if not uid:
        return jsonify({"accounts": True, "user": None, "inviteOnly": invite_only})
    return jsonify({"accounts": True, "inviteOnly": invite_only,
                    "user": {"id": uid, "email": session.get("email") or ""}})


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
