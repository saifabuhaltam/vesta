#!/usr/bin/env python3
"""Build the Cloudflare Pages bundle.

`static/index.html` stays the single source of truth and is never edited for the
cloud: it runs against the local Flask server exactly as before. This script takes
that same file and injects the cloud scripts ahead of the app's own, which is what
flips the backend switch inside it.

    python3 cloud/build.py
    npx wrangler pages deploy cloud/dist --project-name vesta

Anything already in cloud/dist is replaced.
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "static", "index.html")
APP = os.path.join(HERE, "app")
DIST = os.path.join(HERE, "dist")

# Pinned rather than floating on @2: a surprise major version in a CDN URL is a
# bad way to find out your app stopped loading.
SUPABASE_CDN = "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.58.0/dist/umd/supabase.js"

SCRIPTS = ["vesta-cloud.js", "vesta-auth.js", "vesta-files.js", "vesta-notes.js"]

INJECT = (
    "<!-- cloud backend: injected by cloud/build.py, absent from the local app -->\n"
    f'<script src="{SUPABASE_CDN}"></script>\n'
    '<script src="config.js"></script>\n'
    + "".join(f'<script src="{name}"></script>\n' for name in SCRIPTS)
)

# Pages serves these as real response headers.
HEADERS = """/*
  X-Frame-Options: DENY
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  Permissions-Policy: geolocation=(), microphone=(), camera=()

/config.js
  Cache-Control: no-store
"""


def main():
    if not os.path.exists(SRC):
        sys.exit(f"Missing {SRC}")

    html = open(SRC, encoding="utf-8").read()

    # The app lives in the first <script> with no src. Everything cloud has to be
    # defined before it runs, because it reads window.VESTA_BACKEND at startup.
    marker = "<script>\n(function(){"
    if marker not in html:
        sys.exit("Could not find the app's opening script tag; has index.html changed shape?")
    html = html.replace(marker, INJECT + marker, 1)

    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
    os.makedirs(DIST)

    with open(os.path.join(DIST, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(html)

    for name in SCRIPTS:
        shutil.copy2(os.path.join(APP, name), os.path.join(DIST, name))

    # brand assets live next to index.html in both the local app and the deploy
    for name in ("vesta-mark.png", "vesta-favicon.png"):
        src = os.path.join(ROOT, "static", name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(DIST, name))

    config = os.path.join(APP, "config.js")
    if os.path.exists(config):
        # A stray character at the top of this file throws on line 1, which stops
        # window.VESTA_CONFIG ever being assigned. The app then silently falls back
        # to the local server and shows "Can't reach the server" with no clue why.
        # Cheap to check here, genuinely confusing to debug in a browser.
        text = open(config, encoding="utf-8").read()
        head = text.lstrip()
        if not (head.startswith("/*") or head.startswith("//") or head.startswith("window.")):
            sys.exit(
                f"cloud/app/config.js starts with unexpected text: {head[:40]!r}\n"
                "It must begin with a comment or with 'window.VESTA_CONFIG'.\n"
                "Something was typed into the file by accident. Remove it and rebuild."
            )
        if "window.VESTA_CONFIG" not in text:
            sys.exit("cloud/app/config.js never assigns window.VESTA_CONFIG.")
        for placeholder in ("YOUR-PROJECT-REF", "YOUR-ANON-KEY", "YOUR-SUBDOMAIN"):
            if placeholder in text:
                sys.exit(f"cloud/app/config.js still contains the placeholder {placeholder}.")
        shutil.copy2(config, os.path.join(DIST, "config.js"))
        print("  config.js included and checked")
    else:
        # Ship a stub so the page does not 404 and the app falls back cleanly.
        with open(os.path.join(DIST, "config.js"), "w") as fh:
            fh.write(
                "// No cloud config was present at build time.\n"
                "// Copy cloud/app/config.example.js to cloud/app/config.js and rebuild.\n"
                "console.warn('Vesta: no cloud config; the app has nothing to connect to.');\n"
            )
        print("  WARNING: cloud/app/config.js is missing. Built with a stub, so the")
        print("           deployed page will not connect to anything until you add it.")

    with open(os.path.join(DIST, "_headers"), "w") as fh:
        fh.write(HEADERS)

    total = sum(
        os.path.getsize(os.path.join(DIST, f)) for f in os.listdir(DIST)
    )
    print(f"Built {DIST} ({len(os.listdir(DIST))} files, {total // 1024} KB)")
    print("Deploy:  npx wrangler pages deploy cloud/dist --project-name vesta")


if __name__ == "__main__":
    main()
