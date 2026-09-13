#!/usr/bin/env python3
"""Prove two accounts on the LIVE deployment cannot see each other.

This is the check that matters before anyone else is let in. Everything else is
tested against a local database; this one talks to your real Supabase project the
way a browser would, using only the public anon key.

    python3 cloud/setup/test_two_accounts.py a@example.com pass1 b@example.com pass2 --wait

This project requires email confirmation, so --wait pauses after creating each
account to give you time to click the link in your inbox.

Both addresses must already be on the invite list:

    insert into allowed_emails (email, note) values
      ('a@example.com','test A'), ('b@example.com','test B');

Gmail plus-addresses work and land in your normal inbox, so
you+test1@gmail.com and you+test2@gmail.com are fine for this.

It creates one class as A, checks B cannot see or touch it, then deletes it. It
leaves the two accounts in place; remove them from Supabase → Authentication →
Users when you are done.
"""
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "..", "app", "config.js")
GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
ctx = ssl.create_default_context()
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

results = []


def ok(msg, cond, detail=""):
    results.append(cond)
    mark = f"{GREEN}✓{RESET}" if cond else f"{RED}✗{RESET}"
    print(f"  {mark} {msg}" + (f"  {DIM}{detail}{RESET}" if detail else ""))


def call(url, method="GET", headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"User-Agent": UA, "Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw}
    except Exception as e:
        return None, {"raw": str(e)}


def read_config():
    # Env vars win, so this same script can be pointed at a test double to prove
    # the assertions themselves work before anyone touches the live project.
    if os.environ.get("VESTA_SUPABASE_URL"):
        return (os.environ["VESTA_SUPABASE_URL"].rstrip("/"),
                os.environ.get("VESTA_SUPABASE_ANON_KEY", "test-anon-key"))
    text = open(CONFIG).read()
    def f(name):
        m = re.search(name + r"\s*:\s*['\"]([^'\"]*)['\"]", text)
        return m.group(1).strip() if m else ""
    return f("supabaseUrl").rstrip("/"), f("supabaseAnonKey")


def try_sign_in(url, anon, email, password):
    status, body = call(f"{url}/auth/v1/token?grant_type=password",
                        "POST", {"apikey": anon},
                        {"email": email, "password": password})
    if status == 200 and body.get("access_token"):
        return body["access_token"], None
    msg = (body or {}).get("msg") or (body or {}).get("error_description") or ""
    return None, msg


def sign_in(url, anon, email, password, wait_seconds=0):
    """Sign in, creating the account first if it does not exist yet.

    This project requires email confirmation, which is the right default: it proves
    whoever registers an invited address actually controls that mailbox. So after
    creating an account we wait, giving you time to click the link, rather than
    telling you to switch the protection off.
    """
    token, _ = try_sign_in(url, anon, email, password)
    if token:
        return token, "signed in"

    status, body = call(f"{url}/auth/v1/signup", "POST", {"apikey": anon},
                        {"email": email, "password": password})
    if status >= 400:
        msg = body.get("msg") or body.get("error_description") or json.dumps(body)[:120]
        # The signup trigger raises when the email is not allowlisted, and Supabase
        # reports that as a generic database error. Say what it actually means.
        if "Database error saving new user" in msg:
            return None, (f"not on the invite list. Run:  "
                          f"insert into allowed_emails (email) values ('{email}');")
        return None, f"signup refused: {msg}"
    if body.get("access_token"):
        return body["access_token"], "account created"

    if not wait_seconds:
        return None, ("account created, but the confirmation email must be clicked. "
                      "Re-run with --wait and click the link in your inbox.")

    print(f"    {DIM}confirmation email sent to {email}. Click the link; "
          f"waiting up to {wait_seconds // 60} min…{RESET}")
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        time.sleep(5)
        token, _ = try_sign_in(url, anon, email, password)
        if token:
            return token, "confirmed and signed in"
        left = int(deadline - time.time())
        print(f"\r    {DIM}still waiting… {left}s left{RESET}   ", end="", flush=True)
    print()
    return None, "the confirmation link was not clicked in time"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    wait = 300 if "--wait" in sys.argv else 0
    if len(args) != 4:
        print(__doc__)
        sys.exit(2)
    email_a, pass_a, email_b, pass_b = args
    url, anon = read_config()
    if not url or not anon:
        sys.exit("cloud/app/config.js is not filled in.")

    print(f"\nDeployment: {url}\n")

    tok_a, how_a = sign_in(url, anon, email_a, pass_a, wait)
    ok(f"account A ready  ({email_a})", bool(tok_a), how_a)
    tok_b, how_b = sign_in(url, anon, email_b, pass_b, wait)
    ok(f"account B ready  ({email_b})", bool(tok_b), how_b)
    if not (tok_a and tok_b):
        print(f"\n{RED}Cannot continue without both accounts.{RESET}")
        sys.exit(1)

    def hdr(tok):
        return {"apikey": anon, "Authorization": f"Bearer {tok}"}

    rest = f"{url}/rest/v1"

    # Both workspaces should start empty.
    s, a_classes = call(f"{rest}/classes?select=id", headers=hdr(tok_a))
    s, b_classes = call(f"{rest}/classes?select=id", headers=hdr(tok_b))
    ok("A starts with an empty workspace", a_classes == [], str(a_classes)[:60])
    ok("B starts with an empty workspace", b_classes == [], str(b_classes)[:60])

    # Each account gets its own semester from the signup trigger.
    s, a_sem = call(f"{rest}/semesters?select=id,name", headers=hdr(tok_a))
    ok("A was given a semester on signup", isinstance(a_sem, list) and len(a_sem) == 1,
       str(a_sem)[:70])

    # A creates something.
    s, created = call(f"{rest}/classes", "POST",
                      dict(hdr(tok_a), Prefer="return=representation"),
                      {"code": "TEST 101", "name": "Isolation check"})
    made = isinstance(created, list) and created
    ok("A can create a class", bool(made), f"status {s}")
    if not made:
        print(json.dumps(created)[:300])
        sys.exit(1)
    class_id = created[0]["id"]

    # The whole point.
    s, a_sees = call(f"{rest}/classes?select=id,code", headers=hdr(tok_a))
    ok("A sees their own class", len(a_sees) == 1, str(a_sees)[:70])

    s, b_sees = call(f"{rest}/classes?select=id,code", headers=hdr(tok_b))
    ok("B still sees nothing", b_sees == [], str(b_sees)[:70])

    s, direct = call(f"{rest}/classes?id=eq.{class_id}&select=*", headers=hdr(tok_b))
    ok("B cannot fetch A's class by its id", direct == [], str(direct)[:70])

    s, _ = call(f"{rest}/classes?id=eq.{class_id}", "PATCH",
                dict(hdr(tok_b), Prefer="return=representation"), {"name": "hijacked"})
    s2, after = call(f"{rest}/classes?id=eq.{class_id}&select=name", headers=hdr(tok_a))
    ok("B cannot rename A's class",
       after and after[0]["name"] == "Isolation check", str(after)[:70])

    s, _ = call(f"{rest}/classes?id=eq.{class_id}", "DELETE", hdr(tok_b))
    s2, after = call(f"{rest}/classes?id=eq.{class_id}&select=id", headers=hdr(tok_a))
    ok("B cannot delete A's class", len(after) == 1, str(after)[:70])

    # B cannot plant a row owned by A either.
    s, body = call(f"{rest}/classes", "POST", hdr(tok_b),
                   {"code": "FAKE", "user_id": "00000000-0000-0000-0000-000000000000"})
    ok("B cannot create a row owned by someone else", s >= 400, f"status {s}")

    # And the invite list stays invisible.
    s, body = call(f"{rest}/allowed_emails?select=email", headers=hdr(tok_a))
    ok("the invite list is hidden from signed-in users", s >= 400 or body == [], f"status {s}")

    # tidy up
    call(f"{rest}/classes?id=eq.{class_id}", "DELETE", hdr(tok_a))
    s, left = call(f"{rest}/classes?select=id", headers=hdr(tok_a))
    ok("test data cleaned up", left == [], str(left)[:40])

    print()
    if all(results):
        print(f"{GREEN}All {len(results)} checks passed. Two accounts, two separate worlds.{RESET}")
        print(f"{DIM}Remove the test users from Supabase → Authentication → Users when done.{RESET}")
        sys.exit(0)
    print(f"{RED}{results.count(False)} of {len(results)} checks failed.{RESET}")
    sys.exit(1)


if __name__ == "__main__":
    main()
