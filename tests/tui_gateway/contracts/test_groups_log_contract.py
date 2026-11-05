from tui_gateway.contracts import registry


def test_groups_log_accepts_member_actor_profile_in_strict_mode(monkeypatch):
    monkeypatch.setattr(registry, "STRICT", True)

    registry.check_result(registry.METHODS["groups.log"], {
        "events": [{
            "room_id": "room", "seq": 1, "event_id": "event", "kind": "message.member",
            "actor": {"kind": "member", "id": "member", "profile": "solara", "display_name": "Solara"},
            "payload": {"text": "Hello"}, "created_at": 1.0,
        }],
        "cursor": 1, "latest_seq": 1, "has_more": False,
        "authority": {"gateway_id": "gateway", "epoch": 1},
    })
