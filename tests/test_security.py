"""The fixes from the 2026-09-25 security check: headers, stored files, links, sign-in limit."""
import io

import auth as vesta_auth
import week
from app import web_url
from test_auth import FakeSupabase, supa, client, sign_in     # noqa: F401


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------
def test_every_response_carries_the_security_headers(client):
    r = client.get("/")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_hsts_only_over_https(client):
    assert "Strict-Transport-Security" not in client.get("/").headers
    r = client.get("/", headers={"X-Forwarded-Proto": "https"})
    assert r.headers["Strict-Transport-Security"].startswith("max-age=")


# ---------------------------------------------------------------------------
# Stored files
# ---------------------------------------------------------------------------
def upload(client, name, body, mimetype):
    r = client.post("/api/materials", data={"file": (io.BytesIO(body), name, mimetype)},
                    content_type="multipart/form-data")
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


SVG = (b'<svg xmlns="http://www.w3.org/2000/svg">'
       b'<script>fetch("/api/state")</script></svg>')


def test_an_svg_is_served_in_a_sandbox(client, supa):
    sign_in(client, supa)
    mid = upload(client, "drawing.svg", SVG, "image/svg+xml")
    r = client.get(f"/api/materials/{mid}/download")
    assert r.status_code == 200
    assert r.headers["Content-Security-Policy"] == "sandbox"
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_a_type_claimed_by_the_browser_is_still_sandboxed(client, supa):
    sign_in(client, supa)
    mid = upload(client, "photo.png", SVG, "image/svg+xml")
    assert client.get(f"/api/materials/{mid}/download").headers[
        "Content-Security-Policy"] == "sandbox"


def test_a_pdf_is_not_sandboxed_so_the_viewer_still_renders_it(client, supa):
    sign_in(client, supa)
    mid = upload(client, "notes.pdf", b"%PDF-1.4\n%%EOF\n", "application/pdf")
    r = client.get(f"/api/materials/{mid}/download")
    assert "Content-Security-Policy" not in r.headers
    assert "attachment" not in r.headers.get("Content-Disposition", "")


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------
def test_web_url_keeps_web_addresses_and_refuses_the_rest():
    assert web_url("https://canvas.sfu.ca/x") == "https://canvas.sfu.ca/x"
    assert web_url("HTTP://Example.com") == "HTTP://Example.com"
    assert web_url("example.com/page") == "https://example.com/page"
    assert web_url("localhost:3000/x") == "https://localhost:3000/x"
    assert web_url("javascript:alert(1)") is None
    assert web_url("  JavaScript:alert(1)") is None
    assert web_url("data:text/html,<script>1</script>") is None


def test_a_javascript_link_is_refused_on_save(client, supa):
    sign_in(client, supa)
    r = client.post("/api/materials", json={"url": "javascript:alert(document.cookie)"})
    assert r.status_code == 400


def test_a_bare_domain_is_saved_as_https(client, supa):
    sign_in(client, supa)
    r = client.post("/api/materials", json={"url": "example.com/reading"})
    assert r.status_code == 201
    state = client.get("/api/state").get_json()
    urls = [m["url"] for m in state["unfiled"]["materials"]]
    assert "https://example.com/reading" in urls


def test_a_canvas_external_link_that_is_not_a_web_address_is_dropped():
    assert week.web_link("https://example.com") == "https://example.com"
    assert week.web_link("javascript:alert(1)") is None
    assert week.web_link(None) is None


# ---------------------------------------------------------------------------
# Sign-in rate limit
# ---------------------------------------------------------------------------
def test_sign_in_is_refused_after_too_many_tries(client, supa):
    supa.users["saif@example.com"] = "hunter2222"
    wrong = {"email": "saif@example.com", "password": "wrong"}
    for _ in range(vesta_auth.AUTH_MAX_TRIES):
        assert client.post("/api/auth/login", json=wrong).status_code == 401
    r = client.post("/api/auth/login", json=wrong)
    assert r.status_code == 429
    assert "Too many attempts" in r.get_json()["error"]
    # The limited attempt never reached Supabase.
    assert len([c for c in supa.calls if "grant_type=password" in c[1]]) == vesta_auth.AUTH_MAX_TRIES


def test_the_limit_is_per_address(client, supa):
    wrong = {"email": "saif@example.com", "password": "wrong"}
    for _ in range(vesta_auth.AUTH_MAX_TRIES + 1):
        client.post("/api/auth/login", json=wrong, headers={"CF-Connecting-IP": "203.0.113.9"})
    supa.users["joe@example.com"] = "hunter2222"
    r = client.post("/api/auth/login", json={"email": "joe@example.com", "password": "hunter2222"},
                    headers={"CF-Connecting-IP": "198.51.100.4"})
    assert r.status_code == 200


def test_the_limit_lifts_once_the_window_passes(client, supa, monkeypatch):
    wrong = {"email": "saif@example.com", "password": "wrong"}
    for _ in range(vesta_auth.AUTH_MAX_TRIES):
        client.post("/api/auth/login", json=wrong)
    assert client.post("/api/auth/login", json=wrong).status_code == 429
    later = vesta_auth.time.time() + vesta_auth.AUTH_WINDOW_SECONDS + 1
    monkeypatch.setattr(vesta_auth.time, "time", lambda: later)
    assert client.post("/api/auth/login", json=wrong).status_code == 401
