#!/usr/bin/env python3
"""Check that your Vesta cloud setup actually works.

Run it after each stage. It uses nothing but the Python that already ships with
macOS, so there is no tooling to install.

    python3 cloud/setup/verify.py

It reads your keys from cloud/app/config.js, so fill that in first.
"""
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "..", "app", "config.js")

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
ctx = ssl.create_default_context()

passed, failed, skipped = [], [], []


def ok(msg, detail=""):
    passed.append(msg)
    print(f"  {GREEN}✓{RESET} {msg}" + (f"  {DIM}{detail}{RESET}" if detail else ""))


def bad(msg, fix):
    failed.append((msg, fix))
    print(f"  {RED}✗{RESET} {msg}\n      {YELLOW}→ {fix}{RESET}")


def skip(msg, why):
    skipped.append(msg)
    print(f"  {DIM}– {msg} ({why}){RESET}")


# Cloudflare's bot protection rejects Python's default user agent with error 1010,
# which looks alarmingly like a broken Worker. Present a normal browser instead.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def get(url, headers=None, method="GET", data=None, timeout=15):
    hdrs = {"User-Agent": BROWSER_UA}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, headers=hdrs, method=method, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, str(e)


def read_config():
    if not os.path.exists(CONFIG):
        print(f"{RED}No cloud/app/config.js yet.{RESET}")
        print("  Copy cloud/app/config.example.js to cloud/app/config.js and fill it in.")
        sys.exit(1)
    text = open(CONFIG).read()
    def field(name):
        m = re.search(name + r"\s*:\s*['\"]([^'\"]*)['\"]", text)
        return m.group(1).strip() if m else ""
    return {
        "url": field("supabaseUrl").rstrip("/"),
        "anon": field("supabaseAnonKey"),
        "worker": field("workerUrl").rstrip("/"),
    }


def main():
    cfg = read_config()

    print("\nSupabase")
    if not cfg["url"] or "YOUR-PROJECT" in cfg["url"]:
        bad("Project URL is not filled in", "Paste it from Supabase → Project Settings → API")
    elif not cfg["anon"] or "YOUR-ANON" in cfg["anon"]:
        bad("Anon key is not filled in", "Paste the anon/public key from the same page")
    else:
        status, body = get(cfg["url"] + "/auth/v1/health", {"apikey": cfg["anon"]})
        if status == 200:
            ok("The project is reachable", cfg["url"])
        elif status is None:
            bad(f"Cannot reach {cfg['url']}", f"Check the URL. ({body[:60]})")
        else:
            bad(f"The project answered {status}", "Check the anon key is the right one")

        # The tables must exist and must refuse an anonymous reader.
        status, body = get(
            cfg["url"] + "/rest/v1/classes?select=id&limit=1",
            {"apikey": cfg["anon"], "Authorization": "Bearer " + cfg["anon"]},
        )
        if status == 200 and body.strip() == "[]":
            bad("Anyone can read your classes table",
                "Row Level Security is not on. Re-run cloud/setup/all_in_one.sql")
        elif status in (401, 403) or "permission denied" in body.lower() or "JWT" in body:
            ok("Signed-out visitors are refused", "Row Level Security is doing its job")
        elif status == 404:
            bad("The classes table does not exist",
                "Run cloud/setup/all_in_one.sql in the Supabase SQL editor")
        elif status == 200:
            bad("The classes table returned data without a login",
                "Row Level Security is not on. Re-run cloud/setup/all_in_one.sql")
        else:
            bad(f"Unexpected answer {status} from the database", body[:90])

        # allowed_emails must be invisible even to a logged-in user
        status, body = get(
            cfg["url"] + "/rest/v1/allowed_emails?select=email",
            {"apikey": cfg["anon"], "Authorization": "Bearer " + cfg["anon"]},
        )
        if status == 200 and body.strip() not in ("[]", ""):
            bad("The invite list is readable", "Re-run cloud/setup/all_in_one.sql")
        else:
            ok("The invite list is not readable by clients")

    print("\nCloudflare Worker")
    if not cfg["worker"] or "YOUR-SUBDOMAIN" in cfg["worker"]:
        skip("Worker checks", "workerUrl not filled in yet")
    else:
        status, body = get(cfg["worker"] + "/health")
        if status == 200:
            ok("The Worker is deployed and answering", cfg["worker"])
        elif status is None:
            bad(f"Cannot reach {cfg['worker']}", f"Check the URL. ({body[:60]})")
        else:
            bad(f"The Worker answered {status} on /health", body[:90])

        # An unauthenticated upload must be refused.
        status, body = get(
            cfg["worker"] + "/uploads", method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"filename": "x.pdf", "size": 10}).encode(),
        )
        if status == 401:
            ok("Uploads require a signed-in user")
        elif status is None:
            bad("Could not test the upload endpoint", body[:70])
        else:
            bad(f"An unauthenticated upload got {status}, expected 401",
                "Check the Worker deployed the current src/index.js")

        # A forged download token must be refused.
        status, body = get(
            cfg["worker"] + "/files/00000000-0000-0000-0000-000000000000?t=forged.1.aa"
        )
        if status in (401, 403):
            ok("Forged file links are refused")
        elif status == 404:
            ok("Forged file links get nothing back")
        else:
            bad(f"A forged file link got {status}", "Expected it to be refused")

    print("\nBuilt app")
    dist = os.path.join(HERE, "..", "dist", "index.html")
    if not os.path.exists(dist):
        bad("cloud/dist has not been built", "Run: python3 cloud/build.py")
    else:
        html = open(dist, encoding="utf-8").read()
        if "vesta-cloud.js" in html and "supabase-js" in html:
            ok("The built page includes the cloud scripts")
        else:
            bad("The built page is missing the cloud scripts", "Re-run: python3 cloud/build.py")
        cfg_path = os.path.join(HERE, "..", "dist", "config.js")
        built = open(cfg_path).read() if os.path.exists(cfg_path) else ""
        # The build writes a placeholder when cloud/app/config.js is missing, so
        # check the built file actually carries the project this run is testing.
        if cfg["url"] and cfg["url"] in built:
            ok("The build carries your real config")
        elif "no cloud config" in built.lower():
            bad("The build has no config in it",
                "Fill in cloud/app/config.js, then re-run: python3 cloud/build.py")
        else:
            bad("The built config does not match cloud/app/config.js",
                "Re-run: python3 cloud/build.py")

    print()
    if failed:
        n = len(failed)
        print(f"{RED}{n} step{'' if n == 1 else 's'} still to fix.{RESET} {len(passed)} already working.")
        sys.exit(1)
    print(f"{GREEN}All {len(passed)} checks passed.{RESET}"
          + (f" {len(skipped)} skipped." if skipped else ""))


if __name__ == "__main__":
    main()
