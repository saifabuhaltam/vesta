"""/health has to answer the one question that decides whether any of this is real."""
import app as vesta_app


def test_health_reports_that_rls_is_actually_enforced():
    """True here because the suite connects as a non-superuser without BYPASSRLS.

    If a deployment reports false, every policy in `pg_schema.sql` is decoration and
    the accounts are not isolated at all, however green these tests are.
    """
    with vesta_app.app.test_client() as c:
        body = c.get("/health").get_json()
    assert body["database"] == "postgres"
    assert body["rlsEnforced"] is True
    assert body["connectsAsSuperuser"] is False
    assert body["tables"] > 20


def test_health_reports_the_global_ai_ceiling(monkeypatch):
    """Unset must read as null, not as zero, or "no cap" and "cap of nothing" look alike."""
    import ai
    with vesta_app.app.test_client() as c:
        monkeypatch.setattr(ai, "GLOBAL_CAP_USD", None)
        assert c.get("/health").get_json()["aiGlobalDailyCapUsd"] is None
        monkeypatch.setattr(ai, "GLOBAL_CAP_USD", 5.0)
        assert c.get("/health").get_json()["aiGlobalDailyCapUsd"] == 5.0


def test_health_says_whether_the_invite_list_is_on(monkeypatch):
    with vesta_app.app.test_client() as c:
        monkeypatch.delenv("INVITE_EMAILS", raising=False)
        assert c.get("/health").get_json()["inviteOnly"] is False
        monkeypatch.setenv("INVITE_EMAILS", "saif@example.com")
        assert c.get("/health").get_json()["inviteOnly"] is True
