"""message.complete carries a real image_routing receipt for group-chat vision chips."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import patch

from tui_gateway import prompt_turn
import tui_gateway.server as srv


def test_route_turn_images_records_native_receipt(tmp_path, monkeypatch):
    agent = SimpleNamespace(
        provider="ollama",
        model="gemma-vision",
        requested_provider="ollama",
        api_mode="",
    )
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    monkeypatch.setattr(
        prompt_turn,
        "_active_image_routing_identity",
        lambda _agent: ("ollama", "gemma-vision"),
        raising=False,
    )

    with (
        patch("agent.image_routing.decide_image_input_mode", return_value="native"),
        patch(
            "agent.image_routing.build_native_content_parts",
            return_value=(
                [
                    {"type": "text", "text": "hi"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}},
                ],
                [],
            ),
        ),
        patch("hermes_cli.config.load_config", return_value={}),
    ):
        parts = prompt_turn._route_turn_images(agent, "hi", [str(png)])

    assert any(p.get("type") == "image_url" for p in parts)
    assert agent._turn_image_routing == {
        "mode": "native",
        "count": 1,
        "attached": 1,
    }


def test_route_turn_images_records_text_fallback_receipt(monkeypatch):
    agent = SimpleNamespace(
        provider="ollama",
        model="text-only",
        requested_provider="ollama",
        api_mode="",
    )
    monkeypatch.setattr(
        prompt_turn,
        "_active_image_routing_identity",
        lambda _agent: ("ollama", "text-only"),
        raising=False,
    )

    with (
        patch("agent.image_routing.decide_image_input_mode", return_value="text"),
        patch("hermes_cli.config.load_config", return_value={}),
    ):
        monkeypatch.setattr(
            prompt_turn,
            "_build_image_ref_message",
            lambda prompt, images: "hi\n[image: x]",
            raising=False,
        )
        out = prompt_turn._route_turn_images(agent, "hi", ["/tmp/x.png"])

    assert out == "hi\n[image: x]"
    assert agent._turn_image_routing["mode"] == "text"
    assert agent._turn_image_routing["count"] == 1
    assert agent._turn_image_routing.get("reason")


def test_complete_turn_payload_copies_and_clears_image_routing(monkeypatch):
    monkeypatch.setattr(srv, "_get_usage", lambda _agent: {})
    monkeypatch.setattr(srv, "render_message", lambda _text, _cols: None)
    monkeypatch.setattr(srv, "_clear_inflight_turn", lambda _session: None)

    agent = SimpleNamespace(
        _session_title_hint="Scratch",
        _turn_image_routing={"mode": "native", "count": 2, "attached": 2},
    )
    session = {
        "pending_title": None,
        "session_key": "k",
        "history_lock": contextlib.nullcontext(),
        "agent": agent,
    }
    st = SimpleNamespace(
        result={"final_response": "ok"},
        agent=agent,
        terminal_callback=None,
        receipt_committed=True,
        receipt_attempted=False,
        marker_key="",
        error_retained=False,
        error_detail="",
        prompt_text="ping",
    )

    payload, _, status = srv._complete_turn_payload(session, st, None, 80)

    assert status == "complete"
    assert payload["image_routing"] == {"mode": "native", "count": 2, "attached": 2}
    assert not getattr(agent, "_turn_image_routing", None)
