"""Windows Event Log filters: an empty policy list must not silence agents.

The agent runs exactly the channel/level queries /agents/config lists, and a
list replaces its defaults. Every policy stored [] (the column default), so
agents counted events only until their first config fetch: on production all
155 Windows servers had no event-log rows after the day they enrolled.

* /agents/config sends the default filters for a policy without its own;
* the config ETag changes, so agents holding the old one fetch it again;
* the policy API refuses filters the agent would drop as a whole.
"""

from __future__ import annotations

import pytest

from app.services import event_log_policy as elp
from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db_module  # noqa: F401  (fixtures)

DEFAULTS = [{"channel": "System", "levels": ["critical", "error", "warning"]},
            {"channel": "Application", "levels": ["critical", "error", "warning"]}]


# ── The policy module ────────────────────────────────────────────────

def test_empty_or_missing_filters_mean_the_defaults():
    assert elp.effective_filters([]) == DEFAULTS
    assert elp.effective_filters(None) == DEFAULTS
    # Each call returns a fresh copy: a caller mutating it cannot change the next agent's config.
    elp.effective_filters([])[0]["levels"].append("verbose")
    assert elp.effective_filters([]) == DEFAULTS


def test_own_filters_are_sent_normalised():
    got = elp.effective_filters([{"channel": " Security ", "levels": ["Information", "info"], "ids": ["4625", 4740, 4625]}])
    assert got == [{"channel": "Security", "levels": ["information"], "ids": [4625, 4740]}]


def test_a_stored_list_the_agent_would_reject_falls_back_to_the_defaults():
    assert elp.effective_filters([{"channel": "System", "levels": ["loud"]}]) == DEFAULTS


@pytest.mark.parametrize("bad, words", [
    ([{"channel": "", "levels": ["error"]}], "no channel"),
    ([{"channel": "System", "levels": []}], "no levels"),
    ([{"channel": "System", "levels": ["fatal"]}], "not one of"),
    ([{"channel": "System", "levels": ["error"]}, {"channel": "system", "levels": ["error"]}], "twice"),
    ([{"channel": "System", "levels": ["error"], "ids": [0]}], "out of range"),
    ([{"channel": "System", "levels": ["error"], "ids": ["x"]}], "not a number"),
    ([{"channel": f"C{i}", "levels": ["error"]} for i in range(33)], "At most 32"),
    ([{"channel": "System", "levels": ["error"], "ids": list(range(1, 66))}], "more than 64"),
    (["System"], "must be an object"),
])
def test_validation_matches_the_agent_limits(bad, words):
    with pytest.raises(ValueError, match=words):
        elp.validated_filters(bad)


# ── Through the routes ───────────────────────────────────────────────

@pytest.fixture(scope="module")
def world(pg_db_module):
    db = pg_db_module
    pid = str(db.scalar("SELECT id FROM agent_policies WHERE name = 'Windows Baseline'"))
    db.execute("UPDATE agent_policies SET event_log_filters = '[]'::jsonb WHERE id = $1", pid)
    return {"db": db, "policy": pid}


def admin(db):
    return api_client(db, role="admin", username="policy-admin", persist_user=True)


@pytest.fixture
def as_agent(world, monkeypatch):
    """/agents/config as an agent on the Windows Baseline policy (agent authentication itself is not under test)."""
    from app.api.v1 import agents

    async def _auth(agent_id, token, db):
        return {"id": "00000000-0000-0000-0000-000000000001", "policy_id": world["policy"],
                "platform": "windows", "update_ring": "stable"}

    monkeypatch.setattr(agents, "_authenticate", _auth)
    return world


def test_agent_config_sends_the_defaults_for_an_empty_policy(as_agent):
    with admin(as_agent["db"]) as c:
        r = c.get("/api/v1/agents/config", headers={"X-Agent-Id": "a", "Authorization": "Bearer t"})
        assert r.status_code == 200, r.text
        assert r.json()["event_log_filters"] == DEFAULTS, "never [] (the agent would query no channel)"
        etag = r.json()["etag"]
        # An agent that fetched before this fix holds the pre-fix ETag; the new one differs, so it fetches again.
        assert c.get("/api/v1/agents/config", headers={"X-Agent-Id": "a", "Authorization": "Bearer t",
                                                       "If-None-Match": f'"{etag}"'}).status_code == 304


def test_config_etag_carries_the_revision(as_agent, monkeypatch):
    from app.api.v1 import agents
    with admin(as_agent["db"]) as c:
        before = c.get("/api/v1/agents/config", headers={"X-Agent-Id": "a", "Authorization": "Bearer t"}).json()["etag"]
        monkeypatch.setattr(agents.event_log_policy, "CONFIG_REVISION", "pre-fix")
        after = c.get("/api/v1/agents/config", headers={"X-Agent-Id": "a", "Authorization": "Bearer t"}).json()["etag"]
        assert before != after


def test_policy_api_validates_and_stores_filters(world):
    db, pid = world["db"], world["policy"]
    with admin(db) as c:
        r = c.patch(f"/api/v1/agent-policies/{pid}", json={"event_log_filters": [{"channel": "System", "levels": ["fatal"]}]})
        assert r.status_code == 422 and "not one of" in r.json()["detail"]
        r = c.patch(f"/api/v1/agent-policies/{pid}", json={
            "event_log_filters": [{"channel": "Security", "levels": ["Information"], "ids": [4625]}],
            "feature_flags": {"event_log": True}})
        assert r.status_code == 200, r.text
        assert r.json()["event_log_filters"] == [{"channel": "Security", "levels": ["information"], "ids": [4625]}]
        r = c.post("/api/v1/agent-policies", json={"name": "Bad", "event_log_filters": [{"channel": "", "levels": ["error"]}]})
        assert r.status_code == 422
        r = c.post("/api/v1/agent-policies", json={"name": "Quiet SQL", "event_log_filters": [], "feature_flags": {"event_log": False}})
        assert r.status_code == 200, r.text
        assert (r.json()["event_log_filters"], r.json()["feature_flags"]) == ([], {"event_log": False})
        db.execute("UPDATE agent_policies SET event_log_filters = '[]'::jsonb WHERE id = $1", pid)
