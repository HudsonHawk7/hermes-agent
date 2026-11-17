"""A filed UI gate chain completes in order, each card on its own check.

The deadlock this guards: a proof card whose contract is the bare PR URL
waits for every repository-required check, including the statuses its own
children post. Those children cannot start until the proof card is done.
"""

from __future__ import annotations

import hermes_state_wal  # home-I/O guard
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_pr_acceptance as acc
from hermes_cli.kanban_gate_chain import file_ui_gate_chain

HEAD = "a" * 40
OLD = "b" * 40
URL = "https://github.com/acme/widget/pull/7"
REQUIRED = ("force/ui-proof", "force/ui-verified", "force/code-review")


def _world(**over):
    world = {
        "sha": HEAD,
        "state": "OPEN",
        "required": list(REQUIRED),
        "checks": {
            "force/ui-proof": {"conclusion": "success", "sha": HEAD},
            "force/ui-verified": {"conclusion": None, "sha": HEAD},
            "force/code-review": {"conclusion": None, "sha": HEAD},
        },
    }
    world.update(over)
    return world


def _api(world):
    def call(endpoint, *, query=None, paginate=False, profile_home=None):
        if endpoint == "graphql":
            return {"data": {"repository": {"pullRequest": {
                "headRefOid": world["sha"],
                "baseRefName": "develop",
                "state": world["state"],
                "baseRef": {"branchProtectionRule": {"requiredStatusChecks": [
                    {"context": name, "app": None} for name in world["required"]
                ]}},
            }}}}
        if "/rules/" in endpoint:
            return []
        if "check-runs" in endpoint:
            runs = [
                {
                    "id": index + 1,
                    "name": name,
                    "conclusion": info["conclusion"],
                    "status": "completed",
                    "head_sha": info["sha"],
                    "html_url": "https://example.test/run",
                    "app": {"id": 1},
                }
                for index, (name, info) in enumerate(world["checks"].items())
                if info["conclusion"]
            ]
            return [{"check_runs": runs, "total_count": len(runs)}]
        if "/statuses" in endpoint:
            return [[]]
        if "/pulls/" in endpoint:
            merged = world["state"] == "MERGED"
            return {
                "head": {"sha": world["sha"]},
                "base": {"ref": "develop"},
                "state": "closed" if merged else "open",
                "merged": merged,
            }
        raise AssertionError(endpoint)

    return call


def _bind(monkeypatch, world):
    monkeypatch.setattr(acc, "_assignee_profile_home", lambda _assignee: None)
    monkeypatch.setattr(acc, "_api", _api(world))


def test_owned_context_ignores_checks_the_card_does_not_own(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    world = _world()
    _bind(monkeypatch, world)
    receipt = acc.collect_acceptance(f"{URL}#force/ui-proof", URL, assignee="probe")
    assert receipt["ok"] is True, receipt
    assert receipt["classification"] == "success"
    names = {row["name"] for row in receipt["checks"]}
    assert names == {"force/ui-proof"}


def test_old_head_success_does_not_satisfy_an_owned_context(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    world = _world()
    world["checks"]["force/ui-proof"] = {"conclusion": "success", "sha": OLD}
    _bind(monkeypatch, world)
    receipt = acc.collect_acceptance(f"{URL}#force/ui-proof", URL, assignee="probe")
    assert receipt["ok"] is False
    assert receipt["classification"] == "stale"


def test_bare_url_still_requires_every_repository_check(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    _bind(monkeypatch, _world())
    receipt = acc.collect_acceptance(URL, URL, assignee="probe")
    assert receipt["ok"] is False
    assert receipt["classification"] == "missing"


def test_filed_chain_completes_in_order_without_unlinking(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    kb.init_db()
    world = _world()
    _bind(monkeypatch, world)
    with kbc.connect() as conn:
        ids = file_ui_gate_chain(conn, pr_url=URL, title="widget")
        probe, crit, joc = ids["probe"], ids["critiquo"], ids["jocastanu"]
        # The proof card does not parent the cards that post its missing checks.
        children = {
            row["child_id"]
            for row in conn.execute(
                "SELECT child_id FROM task_links WHERE parent_id = ?", (probe,)
            )
        }
        assert crit in children
        assert joc not in children
        assert not kb.complete_task(conn, crit, summary="too soon", metadata={"published_pr": URL})

        assert kb.complete_task(conn, probe, summary="proof posted", metadata={"published_pr": URL})
        kb.recompute_ready(conn)
        # Verify still waits on its own check, not on the proof card.
        assert not kb.complete_task(conn, crit, summary="checks missing", metadata={"published_pr": URL})
        world["checks"]["force/ui-verified"] = {"conclusion": "success", "sha": HEAD}
        assert kb.complete_task(conn, crit, summary="verified", metadata={"published_pr": URL})
        kb.recompute_ready(conn)

        world["checks"]["force/code-review"] = {"conclusion": "success", "sha": HEAD}
        # Merge card needs every required check AND a merged PR.
        assert not kb.complete_task(conn, joc, summary="not merged", metadata={"published_pr": URL})
        world["state"] = "MERGED"
        assert kb.complete_task(conn, joc, summary="merged", metadata={"published_pr": URL})
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute("SELECT id, status FROM tasks WHERE id IN (?,?,?)", (probe, crit, joc))
        }
    assert statuses == {probe: "done", crit: "done", joc: "done"}
