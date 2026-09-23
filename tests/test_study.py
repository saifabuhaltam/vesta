"""Studying: what a grade does, and where the due cards come from.

The buttons under a flashcard used to be decorative. They wrote a date nobody was
shown, nothing collected on the schedule, and Learn and Test never reported what they
found out. These tests hold the three halves of the fix in place: the grades differ
from each other on the very first sighting, a queue can be built across every set at
once, and every mode can feed the same schedule.
"""
import pytest

import ai
from test_auth import FakeSupabase, supa, client, sign_in     # noqa: F401


@pytest.fixture
def signed_in(client, supa):
    sign_in(client, supa)
    return client


@pytest.fixture
def a_class(signed_in):
    r = signed_in.post("/api/classes", json={"name": "Cognition", "code": "PSYC 300"})
    assert r.status_code == 201
    return r.get_json()["id"]


def deck_by_id(client, did):
    """One set out of the listing. The test database is shared across the run, so
    indexing into it would pick up whatever an earlier test left behind."""
    return next(d for d in client.get("/api/decks").get_json() if d["id"] == did)


def make_set(client, class_id, name, terms):
    did = client.post("/api/decks", json={"classId": class_id, "name": name}).get_json()["id"]
    ids = []
    for front, back in terms:
        r = client.post(f"/api/decks/{did}/cards", json={"front": front, "back": back})
        assert r.status_code == 201
        ids.append(r.get_json()["id"])
    return did, ids


# ---------------------------------------------------------------------------
# what a grade actually does
# ---------------------------------------------------------------------------
def test_the_three_grades_give_three_different_dates_on_a_new_card():
    """The complaint that started this: Good and Easy did the same visible thing.

    Plain SM-2 gives a first review the same one-day interval whichever way it went,
    so the button you pressed changed nothing you could see. A miss now comes back
    tomorrow, Good in a day, Easy in four.
    """
    fresh = dict(ease=2.5, reps=0, interval=0, lapses=0)
    missed = ai.sm2_next(grade=1, **fresh)
    good = ai.sm2_next(grade=4, **fresh)
    easy = ai.sm2_next(grade=5, **fresh)
    assert missed["interval"] == 1
    assert good["interval"] == 1
    assert easy["interval"] == 4
    assert len({missed["repetitions"], good["repetitions"]}) == 2   # a miss does not advance


def test_easy_keeps_pulling_ahead_of_good_on_a_mature_card():
    mature = dict(ease=2.5, reps=6, interval=30, lapses=0)
    assert ai.sm2_next(grade=5, **mature)["interval"] > ai.sm2_next(grade=4, **mature)["interval"]


def test_a_miss_resets_the_ladder_and_counts_a_lapse():
    out = ai.sm2_next(ease=2.5, reps=6, interval=90, lapses=1, grade=1)
    assert out["repetitions"] == 0
    assert out["interval"] == 1
    assert out["lapses"] == 2


def test_ease_never_falls_below_the_floor():
    ease = 2.5
    for _ in range(20):
        ease = ai.sm2_next(ease=ease, reps=3, interval=10, lapses=0, grade=3)["ease"]
    assert ease >= 1.3


def test_a_new_card_is_due_immediately(signed_in, a_class):
    """A card written on a Sunday evening in Vancouver was not reviewable until Monday.

    The server stamped today's UTC date on it while the browser counted days locally,
    so the card sat one day in the future. No due date means due now.
    """
    did, ids = make_set(signed_in, a_class, "Memory", [("Encoding", "Information in")])
    card = signed_in.get("/api/decks/" + did).get_json()["cards"][0]
    assert card["due"] is None
    assert deck_by_id(signed_in, did)["dueCount"] == 1


# ---------------------------------------------------------------------------
# one queue across every set
# ---------------------------------------------------------------------------
def test_the_due_queue_spans_every_set_and_names_where_each_card_came_from(signed_in, a_class):
    make_set(signed_in, a_class, "Memory", [("Encoding", "In"), ("Retrieval", "Out")])
    make_set(signed_in, a_class, "Attention", [("Priming", "Earlier exposure")])
    cards = signed_in.get(f"/api/study/queue?scope=due&classId={a_class}").get_json()["cards"]
    assert len(cards) == 3
    assert {c["deckName"] for c in cards} == {"Memory", "Attention"}


def test_a_scheduled_card_leaves_the_due_queue_but_not_the_cram_queue(signed_in, a_class):
    did, ids = make_set(signed_in, a_class, "Memory", [("Encoding", "In"), ("Retrieval", "Out")])
    signed_in.post(f"/api/cards/{ids[0]}/review", json={"grade": 5})
    due = signed_in.get(f"/api/study/queue?scope=due&deckId={did}").get_json()["cards"]
    cram = signed_in.get(f"/api/study/queue?scope=cram&deckId={did}").get_json()["cards"]
    assert [c["id"] for c in due] == [ids[1]]
    assert len(cram) == 2


def test_cram_puts_the_least_known_first(signed_in, a_class):
    did, ids = make_set(signed_in, a_class, "Memory",
                        [("Encoding", "In"), ("Retrieval", "Out"), ("Priming", "Exposure")])
    # Learn says the second one is known; the schedule says the third has been seen.
    signed_in.post(f"/api/decks/{did}/learn", json={"levels": {ids[1]: 2}})
    signed_in.post(f"/api/cards/{ids[2]}/review", json={"grade": 4})
    order = [c["id"] for c in
             signed_in.get(f"/api/study/queue?scope=cram&deckId={did}").get_json()["cards"]]
    assert order[-1] == ids[1]          # the learned one comes last
    assert order.index(ids[0]) < order.index(ids[2])   # never seen before merely seen


def test_the_trouble_queue_holds_only_what_keeps_being_forgotten(signed_in, a_class):
    did, ids = make_set(signed_in, a_class, "Memory", [("Encoding", "In"), ("Retrieval", "Out")])
    for _ in range(ai.STUCK_LAPSES):
        signed_in.post(f"/api/cards/{ids[0]}/review", json={"grade": 1})
    trouble = signed_in.get(f"/api/study/queue?scope=trouble&deckId={did}").get_json()["cards"]
    assert [c["id"] for c in trouble] == [ids[0]]
    assert deck_by_id(signed_in, did)["stuckCount"] == 1


def test_a_queue_can_be_narrowed_to_one_class(signed_in, a_class):
    other = signed_in.post("/api/classes", json={"name": "Discrete", "code": "MACM 101"}).get_json()["id"]
    make_set(signed_in, a_class, "Memory", [("Encoding", "In")])
    make_set(signed_in, other, "Proofs", [("Induction", "Base case then the step")])
    cards = signed_in.get(f"/api/study/queue?scope=cram&classId={other}").get_json()["cards"]
    assert [c["deckName"] for c in cards] == ["Proofs"]


# ---------------------------------------------------------------------------
# every mode feeds the same schedule
# ---------------------------------------------------------------------------
def test_a_batch_of_grades_schedules_every_card_it_names(signed_in, a_class):
    did, ids = make_set(signed_in, a_class, "Memory", [("Encoding", "In"), ("Retrieval", "Out")])
    r = signed_in.post("/api/cards/review-batch", json={"reviews": [
        {"id": ids[0], "grade": 1},
        {"id": ids[1], "grade": 5},
    ]})
    assert r.status_code == 200
    by_id = {c["id"]: c for c in r.get_json()["cards"]}
    assert by_id[ids[0]]["lapses"] == 1
    assert by_id[ids[1]]["interval"] == 4


def test_a_batch_ignores_a_card_that_is_not_there_rather_than_failing(signed_in, a_class):
    """One stale id from a mode that has been open a while should not lose the round."""
    did, ids = make_set(signed_in, a_class, "Memory", [("Encoding", "In")])
    r = signed_in.post("/api/cards/review-batch", json={"reviews": [
        {"id": "gone", "grade": 4},
        {"id": ids[0], "grade": 4},
        {"grade": 4},
    ]})
    assert r.status_code == 200
    assert [c["id"] for c in r.get_json()["cards"]] == [ids[0]]


def test_an_empty_batch_is_not_an_error(signed_in):
    assert signed_in.post("/api/cards/review-batch", json={"reviews": []}).status_code == 200


# ---------------------------------------------------------------------------
# the exam a set is really for
# ---------------------------------------------------------------------------
def test_a_set_reports_the_next_exam_in_its_class(signed_in, a_class):
    from datetime import date, timedelta
    when = (date.today() + timedelta(days=5)).isoformat()
    signed_in.post("/api/items", json={"title": "Midterm 2", "type": "exam",
                                       "classId": a_class, "dueDate": when})
    did, _ = make_set(signed_in, a_class, "Memory", [("Encoding", "In")])
    d = deck_by_id(signed_in, did)
    assert d["examTitle"] == "Midterm 2"
    assert d["examDate"] == when


def test_an_essay_is_not_an_exam_and_a_finished_exam_is_not_either(signed_in, a_class):
    from datetime import date, timedelta
    soon = (date.today() + timedelta(days=3)).isoformat()
    signed_in.post("/api/items", json={"title": "Essay 1", "type": "assignment",
                                       "classId": a_class, "dueDate": soon})
    done = signed_in.post("/api/items", json={"title": "Midterm 1", "type": "exam",
                                              "classId": a_class, "dueDate": soon}).get_json()["id"]
    signed_in.put("/api/items/" + done, json={"status": "done"})
    did, _ = make_set(signed_in, a_class, "Memory", [("Encoding", "In")])
    assert deck_by_id(signed_in, did)["examDate"] is None


def test_how_much_of_a_set_is_learned_is_reported_with_it(signed_in, a_class):
    did, ids = make_set(signed_in, a_class, "Memory",
                        [("Encoding", "In"), ("Retrieval", "Out")])
    signed_in.post(f"/api/decks/{did}/learn", json={"levels": {ids[0]: 2, ids[1]: 1}})
    d = deck_by_id(signed_in, did)
    assert d["cardCount"] == 2
    assert d["learnedCount"] == 1
