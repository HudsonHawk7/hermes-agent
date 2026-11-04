"""Work watch Path A — negative-first AC tests.

Failing behaviours are asserted first (AC-N*). No live Graph / MSAL required.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "work_watch"


def _ensure_pkg():
    """Load plugins.work_watch* from the repo worktree path."""
    root = str(REPO)
    if root not in sys.path:
        sys.path.insert(0, root)
    # plugins is a namespace package on disk
    if "plugins" not in sys.modules:
        pkg = types.ModuleType("plugins")
        pkg.__path__ = [str(REPO / "plugins")]
        sys.modules["plugins"] = pkg
    return importlib.import_module("plugins.work_watch.auth"), importlib.import_module(
        "plugins.work_watch.store"
    ), importlib.import_module("plugins.work_watch.judge"), importlib.import_module(
        "plugins.work_watch.reader"
    ), importlib.import_module("plugins.work_watch.service")


@pytest.fixture()
def ww(tmp_path, monkeypatch):
    hermes = tmp_path / ".hermes"
    hermes.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    # Clear token-related env
    for k in (
        "GRAPH_WATCH_REFRESH_TOKEN",
        "GRAPH_WATCH_CLIENT_ID",
        "GRAPH_WATCH_TENANT",
        "GRAPH_WATCH_IDENTITY_MASKED",
        "GRAPH_WATCH_CLOUD_JUDGE",
    ):
        monkeypatch.delenv(k, raising=False)

    auth, store, judge, reader, service = _ensure_pkg()
    # reset module globals
    auth.set_access_token_memory(None)
    auth.cancel_device_flow()
    service.clear_reader_stop()
    # isolate save/remove env
    env_file = hermes / ".env"
    env_file.write_text("", encoding="utf-8")

    def _save(key, value):
        lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
        out = [ln for ln in lines if not ln.startswith(f"{key}=")]
        out.append(f"{key}={value}")
        env_file.write_text("\n".join(out) + "\n", encoding="utf-8")
        os.environ[key] = value

    def _remove(key):
        if not env_file.exists():
            os.environ.pop(key, None)
            return False
        lines = env_file.read_text(encoding="utf-8").splitlines()
        new = [ln for ln in lines if not ln.startswith(f"{key}=")]
        found = len(new) < len(lines)
        env_file.write_text("\n".join(new) + ("\n" if new else ""), encoding="utf-8")
        os.environ.pop(key, None)
        return found

    monkeypatch.setattr("hermes_cli.config.save_env_value", _save)
    monkeypatch.setattr("hermes_cli.config.remove_env_value", _remove)

    db = tmp_path / "ww.db"
    return types.SimpleNamespace(
        auth=auth,
        store=store,
        judge=judge,
        reader=reader,
        service=service,
        db=str(db),
        hermes=hermes,
        env_file=env_file,
        monkeypatch=monkeypatch,
    )


# ---------------------------------------------------------------------------
# AC-N1 — off / NULL = zero Graph calls
# ---------------------------------------------------------------------------


class TestAC_N1_OffZeroGraph:
    def test_absent_token_status_off_and_revoke_disabled(self, ww):
        snap = ww.auth.current_status()
        assert snap["status"] == "off"
        assert snap["setting_name"] == "GRAPH_WATCH_REFRESH_TOKEN"
        assert snap["has_refresh_token"] is False
        assert snap["revoke_enabled"] is False
        assert snap["null_honest"] is True

    def test_null_token_treated_as_absent(self, ww):
        ww.monkeypatch.setenv("GRAPH_WATCH_REFRESH_TOKEN", "NULL")
        assert ww.auth.has_refresh_token() is False
        assert ww.auth.reader_may_call_graph() is False

    def test_run_once_makes_zero_graph_calls(self, ww):
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            raise AssertionError("Graph must not be called when unsigned")

        out = ww.service.run_once(db_path=ww.db, http_get=boom)
        assert out["graph_calls"] == 0
        assert out.get("skipped") is True
        assert calls["n"] == 0

    def test_fetch_candidates_skips_without_token(self, ww):
        out = ww.reader.fetch_candidates(http_get=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))
        assert out["skipped"] is True
        assert out["graph_calls"] == 0
        assert out["candidates"] == []


# ---------------------------------------------------------------------------
# AC-N2 — revoke with no token is honest no-op
# ---------------------------------------------------------------------------


class TestAC_N2_RevokeNoToken:
    def test_revoke_without_token(self, ww):
        out = ww.auth.revoke()
        assert out["ok"] is True
        assert out["revoked"] is False
        assert out["reason"] == "no_token"
        assert out["status"] == "off"
        assert out["null_honest"] is True


# ---------------------------------------------------------------------------
# AC-N3 — device-code cancel keeps token NULL
# ---------------------------------------------------------------------------


class TestAC_N3_Cancel:
    def test_cancel_leaves_no_token(self, ww):
        ww.monkeypatch.setenv("GRAPH_WATCH_CLIENT_ID", "00000000-0000-0000-0000-000000000001")

        class FakeApp:
            def initiate_device_flow(self, scopes):
                return {
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://microsoft.com/devicelogin",
                    "message": "go",
                    "expires_in": 900,
                }

            def acquire_token_by_device_flow(self, flow):
                import time

                time.sleep(0.05)
                return {"error": "authorization_pending"}

        # Start would spawn poller; we just finish via cancel helper.
        out = ww.auth.finish_device_flow_for_tests(cancelled=True)
        assert out.status == "off"
        assert out.has_refresh_token is False
        assert ww.auth.has_refresh_token() is False
        snap = ww.auth.current_status()
        assert snap["status"] == "off"


# ---------------------------------------------------------------------------
# AC-N4 / AC-N5 — admin consent + consent missing
# ---------------------------------------------------------------------------


class TestAC_N4_N5_AadErrors:
    @pytest.mark.parametrize(
        "code,expect_status",
        [
            ("AADSTS90094", "needs_sign_in"),
            ("AADSTS90095", "needs_sign_in"),
            ("AADSTS65001", "needs_sign_in"),
            ("AADSTS65004", "off"),
        ],
    )
    def test_aad_errors_keep_token_null(self, ww, code, expect_status):
        out = ww.auth.finish_device_flow_for_tests(error_code=code)
        assert out.has_refresh_token is False
        assert ww.auth.has_refresh_token() is False
        assert out.status == expect_status
        assert code in (out.error_code or "")
        plain = ww.auth.plain_words_for_aad(code)
        assert plain
        if code in {"AADSTS90094", "AADSTS90095"}:
            assert "admin" in plain.lower()
        if code == "AADSTS65001":
            assert "accept" in plain.lower() or "consent" in plain.lower()


# ---------------------------------------------------------------------------
# AC-N6 / AC-N7 — access token never on disk; identity masked; no raw refresh in status
# ---------------------------------------------------------------------------


class TestAC_N6_N7_Secrets:
    def test_access_token_memory_only_not_in_env_file(self, ww):
        ww.auth.set_access_token_memory("ACCESS_SECRET_VALUE", 3600)
        text = ww.env_file.read_text(encoding="utf-8")
        assert "ACCESS_SECRET_VALUE" not in text
        snap = ww.auth.current_status()
        blob = json.dumps(snap)
        assert "ACCESS_SECRET_VALUE" not in blob
        assert "refresh" not in blob.lower() or "GRAPH_WATCH_REFRESH_TOKEN" in blob

    def test_refresh_token_not_echoed_in_status(self, ww):
        from hermes_cli.config import save_env_value

        save_env_value("GRAPH_WATCH_REFRESH_TOKEN", "REFRESH_SECRET_VALUE_xyz")
        snap = ww.auth.current_status(identity_hint="judson@contoso.com")
        blob = json.dumps(snap)
        assert "REFRESH_SECRET_VALUE_xyz" not in blob
        assert snap["identity_masked"] == "j***@contoso.com"
        assert snap["has_refresh_token"] is True
        assert snap["status"] == "signed_in"

    def test_mask_identity(self, ww):
        assert ww.auth.mask_identity("a@b.co") == "a***@b.co"
        assert ww.auth.mask_identity(None) is None


# ---------------------------------------------------------------------------
# AC-N8 — revoke clears token and returns Off
# ---------------------------------------------------------------------------


class TestAC_N8_RevokeClears:
    def test_revoke_clears_refresh_and_access(self, ww):
        from hermes_cli.config import save_env_value

        save_env_value("GRAPH_WATCH_REFRESH_TOKEN", "to-clear")
        ww.auth.set_access_token_memory("access", 3600)
        out = ww.auth.revoke()
        assert out["revoked"] is True
        assert out["status"] == "off"
        assert ww.auth.has_refresh_token() is False
        assert ww.auth.get_access_token_memory() is None
        # env file must not retain the value
        assert "to-clear" not in ww.env_file.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AC-N9 — refresh fail stops reader
# ---------------------------------------------------------------------------


class TestAC_N9_StopOnAuthFail:
    def test_reader_stops_when_token_provider_returns_none(self, ww):
        from hermes_cli.config import save_env_value

        save_env_value("GRAPH_WATCH_REFRESH_TOKEN", "present")
        ww.monkeypatch.setenv("GRAPH_WATCH_CLIENT_ID", "cid")

        def no_token():
            return None

        out = ww.service.run_once(db_path=ww.db, token_provider=no_token, http_get=lambda *a, **k: {})
        assert out["ok"] is False
        assert out["status"] == "needs_sign_in"
        assert "sign in again" in (out.get("error_plain") or "").lower()
        # second call still stopped, no spin success
        out2 = ww.service.run_once(db_path=ww.db, token_provider=no_token)
        assert out2["ok"] is False


# ---------------------------------------------------------------------------
# AC-N10 — no client secret
# ---------------------------------------------------------------------------


class TestAC_N10_NoClientSecret:
    def test_start_sign_in_requires_public_client_id_only(self, ww):
        out = ww.auth.start_device_code_sign_in()
        assert out["ok"] is False
        assert "CLIENT_ID" in (out.get("error_plain") or out.get("error_code") or "")
        # no secret env consulted
        assert os.environ.get("GRAPH_WATCH_CLIENT_SECRET") in (None, "")


# ---------------------------------------------------------------------------
# AC-N11 / N12 / N13 — content stays local; board is counts; sqlite local
# ---------------------------------------------------------------------------


class TestAC_DataRules:
    def test_board_summary_counts_only(self, ww):
        with ww.store.open_store(ww.db) as conn:
            ww.store.upsert_flag(
                conn,
                conversation_id="c1",
                source="mail",
                tier=1,
                why="direct_ask:need you",
                title_local="SECRET SUBJECT",
                preview_local="SECRET BODY",
                sender_local="boss@contoso.com",
            )
            summary = ww.store.board_summary(conn)
            flags = ww.store.list_flags(conn, include_local_content=False)
        assert summary["tier_1"] == 1
        assert "SECRET" not in json.dumps(summary)
        assert flags[0]["why"]
        assert "title_local" not in flags[0]
        assert "SECRET" not in json.dumps(flags)

    def test_local_db_path_under_plugin_data(self, ww):
        # default connect uses plugin_data_dir
        with ww.store.open_store() as conn:
            ww.store.record_poll(conn, item_count=0)
        data = ww.hermes / "plugin-data" / "work_watch"
        assert data.exists()


# ---------------------------------------------------------------------------
# AC-N14 / N15 / N16 / N17 — judge + dedupe
# ---------------------------------------------------------------------------


class TestAC_Judge:
    def test_newsletter_dropped_with_receipt(self, ww):
        cand = {
            "id": "m1",
            "conversation_id": "conv-news",
            "source": "mail",
            "subject": "Weekly Newsletter — 20% off",
            "sender": "newsletter@shop.com",
            "preview": "Unsubscribe here",
        }
        j = ww.judge.classify(cand)
        assert j.tier == 3
        assert j.dropped is True
        with ww.store.open_store(ww.db) as conn:
            out = ww.judge.apply_to_store(conn, cand, j)
            assert out is None
            flags = ww.store.list_flags(conn)
            receipts = ww.store.list_tier3_receipts(conn)
        assert flags == []
        assert len(receipts) == 1
        assert receipts[0]["why"]
        assert "20% off" not in json.dumps(receipts)

    def test_tier1_requires_why(self, ww):
        cand = {
            "id": "m2",
            "conversation_id": "conv-ask",
            "source": "mail",
            "subject": "Need you to approve the release",
            "sender": "pm@contoso.com",
            "preview": "please review",
        }
        j = ww.judge.classify(cand)
        assert j.tier == 1
        assert j.why
        with ww.store.open_store(ww.db) as conn:
            row = ww.judge.apply_to_store(conn, cand, j)
        assert row["why"]

    def test_flag_without_why_is_defect(self, ww):
        with ww.store.open_store(ww.db) as conn:
            with pytest.raises(ValueError, match="why"):
                ww.store.upsert_flag(
                    conn, conversation_id="x", source="mail", tier=1, why="  "
                )

    def test_quiet_when_no_signal(self, ww):
        j = ww.judge.classify(
            {
                "subject": "Lunch menu",
                "sender": "friend@example.com",
                "preview": "pizza?",
                "conversation_id": "c",
            }
        )
        assert j.dropped is True

    def test_dedupe_conversation_updates_in_place(self, ww):
        with ww.store.open_store(ww.db) as conn:
            r1 = ww.store.upsert_flag(
                conn, conversation_id="thread-1", source="mail", tier=1, why="direct_ask:a"
            )
            r2 = ww.store.upsert_flag(
                conn, conversation_id="thread-1", source="mail", tier=1, why="direct_ask:b"
            )
            flags = ww.store.list_flags(conn)
        assert r1.id == r2.id
        assert len(flags) == 1
        assert flags[0]["seen_count"] == 2
        assert flags[0]["why"] == "direct_ask:b"


# ---------------------------------------------------------------------------
# AC-P happy plumbing (unit level)
# ---------------------------------------------------------------------------


class TestAC_Plumbing:
    def test_scopes_plain_words_no_admin(self, ww):
        snap = ww.auth.current_status()
        labels = {s["label"] for s in snap["scopes"]}
        assert labels == {
            "Stay signed in",
            "Who you are",
            "Read your inbox",
            "Read your Teams chats",
        }
        assert all(s["admin_consent"] is False for s in snap["scopes"])

    def test_last_check_never_message_text(self, ww):
        with ww.store.open_store(ww.db) as conn:
            ww.store.record_poll(conn, item_count=3)
            last = ww.store.last_check(conn)
        assert last["last_check_count"] == 3
        assert last["message_text"] is None

    def test_poll_interval_active_vs_offhours(self, ww):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        ct = ZoneInfo("America/Chicago")
        noon = datetime(2026, 10, 29, 12, 0, tzinfo=ct)
        night = datetime(2026, 10, 29, 22, 0, tzinfo=ct)
        assert ww.service.poll_interval_minutes(noon) == 10
        assert ww.service.poll_interval_minutes(night) == 60

    def test_successful_poll_with_fake_http(self, ww):
        from hermes_cli.config import save_env_value

        save_env_value("GRAPH_WATCH_REFRESH_TOKEN", "rt")
        ww.monkeypatch.setenv("GRAPH_WATCH_CLIENT_ID", "cid")

        def fake_get(url, headers, params):
            assert "Bearer" in headers.get("Authorization", "")
            assert "rt" not in json.dumps(headers)  # refresh not sent as bearer
            if "messages/delta" in url or "mailFolders" in url:
                return {
                    "value": [
                        {
                            "id": "m1",
                            "conversationId": "c-ask",
                            "subject": "Action required: please approve",
                            "from": {"emailAddress": {"address": "boss@contoso.com"}},
                            "bodyPreview": "need you on this",
                        },
                        {
                            "id": "m2",
                            "conversationId": "c-news",
                            "subject": "Newsletter sale ends",
                            "from": {"emailAddress": {"address": "newsletter@x.com"}},
                            "bodyPreview": "unsubscribe",
                        },
                    ],
                    "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta-next",
                }
            if url.endswith("/me/chats"):
                return {"value": []}
            return {"value": []}

        out = ww.service.run_once(
            db_path=ww.db,
            http_get=fake_get,
            token_provider=lambda: "access-only-in-memory",
        )
        assert out["ok"] is True
        assert out["graph_calls"] >= 1
        assert out["flags_added"] >= 1
        assert out["dropped"] >= 1
        assert out["board_summary"]["total"] >= 1
        # board summary has no subjects
        assert "Newsletter" not in json.dumps(out["board_summary"])


# ---------------------------------------------------------------------------
# AC-N14 store refuses tier-3 flags
# ---------------------------------------------------------------------------


class TestStoreGuards:
    def test_refuse_tier3_flag_row(self, ww):
        with ww.store.open_store(ww.db) as conn:
            with pytest.raises(ValueError, match="tier-3"):
                ww.store.upsert_flag(
                    conn, conversation_id="t3", source="mail", tier=3, why="x"
                )
