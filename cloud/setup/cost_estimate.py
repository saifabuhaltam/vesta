#!/usr/bin/env python3
"""What will Headstart actually cost you?

Uses your real assignments to size the prompts, then multiplies by whatever the
current prices are. Prices change, so it asks you for them rather than baking in
numbers that will quietly go stale.

    python3 cloud/setup/cost_estimate.py

Get the current prices from https://www.anthropic.com/pricing or
https://openai.com/api/pricing and type them in.
"""
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "..", "..", "data", "vesta.db")

# Typical output length per kind, measured from what these prompts actually produce.
KINDS = {
    "explain": 400,
    "quiz_prep": 900,
    "study_outline": 1000,
    "synthesis": 1100,
    "essay_outline": 1200,
    "draft": 2500,
}

PROMPTS = {
    "draft": "Write a first draft a student could build on.",
    "essay_outline": "Produce a structured essay outline with a thesis and section headings.",
    "quiz_prep": "Produce focused quiz preparation: likely question areas and concise answers.",
    "study_outline": "Produce a study outline covering the material this exam is likely to test.",
    "synthesis": "Synthesise the key ideas a student should take from the assigned readings.",
    "explain": "Explain in plain language what this assignment is actually asking for, and how to approach it.",
}


def build(kind, it):
    b = [PROMPTS[kind], "", f"Assignment: {it['title'] or 'Untitled'}",
         f"Type: {it['type'] or 'assignment'}"]
    if it["due_date"]:
        b.append(f"Due: {it['due_date']}")
    if it["weight"] is not None:
        b.append(f"Worth: {it['weight']}% of the course grade")
    if it["notes"]:
        b += ["", "Details the student recorded:", it["notes"]]
    b += ["", "This is study scaffolding for the student to work from, not something to submit as-is."]
    return "\n".join(b)


def ask(label, default):
    raw = input(f"  {label} [{default}]: ").strip()
    if not raw:
        return default
    try:
        return float(raw.replace("$", "").replace(",", ""))
    except ValueError:
        print("    not a number, using the default")
        return default


def main():
    if os.path.exists(DB):
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        items = conn.execute("select * from items").fetchall()
        sizes = [len(build("essay_outline", i)) for i in items]
        in_tokens = int(sum(sizes) / len(sizes) / 4) if sizes else 60
        print(f"\nMeasured from your {len(items)} real assignments:")
        print(f"  input prompt ≈ {in_tokens} tokens")
    else:
        in_tokens = 60
        print("\nNo local database found; assuming a 60 token prompt.")

    print("\nPrices per MILLION tokens, from the provider's pricing page.")
    print("The defaults below are rough placeholders. Check and correct them.\n")
    in_price = ask("input  $ per 1M tokens", 3.0)
    out_price = ask("output $ per 1M tokens", 15.0)

    print("\nCost of one generation:")
    per_kind = {}
    for kind, out_tokens in sorted(KINDS.items(), key=lambda kv: kv[1]):
        cost = (in_tokens / 1e6) * in_price + (out_tokens / 1e6) * out_price
        per_kind[kind] = cost
        print(f"  {kind:<15} {out_tokens:>5} out tokens   ${cost:.4f}")

    avg = sum(per_kind.values()) / len(per_kind)
    print(f"\n  average generation                  ${avg:.4f}")

    print("\nOver a semester:")
    for n in (20, 50, 100, 300):
        print(f"  {n:>3} generations   ${avg * n:.2f}")

    print("\nThe expensive half is output, not input. Your prompts are tiny.")
    print("A hard spend limit in the provider console is the only thing that")
    print("turns this from an unknown into a number you have already agreed to.")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        sys.exit(0)
