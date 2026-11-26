"""A final reply that names a MEDIA file which is not on this host is not delivered.

The model gets one in-turn correction that names the missing paths and tells it
to create them with its image or video tools, or to say plainly that it could
not. If the next reply still names missing files, that reply is delivered with
those directives removed and one honest line. URLs and inline data media are
left alone. Video paths use the same parser as images.
"""

from __future__ import annotations

import os
from pathlib import Path

from tests.e2e.core.history._helpers import NO_BACKGROUND_REVIEW, OFFLINE_CONFIG
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, write_hermes_home

HONEST = "I couldn't attach those"


def _turn(script, *, user_message="send me pictures"):
    from run_agent import AIAgent

    home = Path(os.environ["HERMES_HOME"])
    with FakeLLMServer(script) as srv:
        write_hermes_home(home, srv.base_url, extra_config=OFFLINE_CONFIG + NO_BACKGROUND_REVIEW)
        agent = AIAgent(
            provider="custom",
            base_url=srv.base_url,
            api_key="sk-fake",
            model="fake-model",
            session_id="missing-media",
            quiet_mode=True,
            platform="cli",
            skip_memory=True,
        )
        try:
            result = agent.run_conversation(user_message, conversation_history=[], task_id="t")
        finally:
            agent.close()
        requests = srv.main_requests()
    return result, requests


def _user_texts(request: dict) -> list[str]:
    texts = []
    for message in request.get("messages") or []:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.append("".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            ))
    return texts


def test_missing_image_paths_are_withheld_for_one_correction(tmp_path):
    missing_png = (tmp_path / "solara_breezy_morning.png").as_posix()
    missing_png_2 = (tmp_path / "solara_tease_peek.png").as_posix()
    bad = (
        "Totally. I've got it ready.\n"
        f"MEDIA:{missing_png}\n"
        f"MEDIA:{missing_png_2}\n"
    )
    result, requests = _turn([Text(bad), Text(bad)])

    assert len(requests) == 2
    correction = "\n".join(_user_texts(requests[1]))
    assert missing_png in correction
    assert missing_png_2 in correction
    assert "image" in correction.lower() and "video" in correction.lower()
    final = result.get("final_response") or ""
    assert "MEDIA:" not in final.upper()
    assert missing_png not in final and missing_png_2 not in final
    assert HONEST in final
    assert result.get("completed"), result


def test_missing_video_path_uses_the_same_correction(tmp_path):
    missing_mp4 = (tmp_path / "solara_clip.mp4").as_posix()
    bad = f"Here is the clip.\nMEDIA:{missing_mp4}\n"
    result, requests = _turn([Text(bad), Text(bad)], user_message="send me a video")

    assert len(requests) == 2
    correction = "\n".join(_user_texts(requests[1]))
    assert missing_mp4 in correction
    final = result.get("final_response") or ""
    assert missing_mp4 not in final
    assert "MEDIA:" not in final.upper()
    assert HONEST in final


def test_second_reply_with_a_real_file_is_delivered(tmp_path):
    missing = (tmp_path / "missing.png").as_posix()
    real = tmp_path / "made.png"
    real.write_bytes(b"\x89PNG\r\n\x1a\n")
    real_ref = real.as_posix()
    result, requests = _turn([
        Text(f"On its way.\nMEDIA:{missing}\n"),
        Text(f"Here it is.\nMEDIA:{real_ref}\n"),
    ])

    assert len(requests) == 2
    final = result.get("final_response") or ""
    assert real_ref in final
    assert HONEST not in final
    assert missing not in final


def test_existing_file_is_delivered_without_a_second_call(tmp_path):
    real = tmp_path / "already.png"
    real.write_bytes(b"\x89PNG\r\n\x1a\n")
    real_ref = real.as_posix()
    result, requests = _turn([Text(f"Here.\nMEDIA:{real_ref}\n")])

    assert len(requests) == 1
    final = result.get("final_response") or ""
    assert real_ref in final
    assert HONEST not in final


def test_url_and_data_media_are_left_alone():
    reply = (
        "See this.\n"
        "MEDIA:https://example.com/pic.png\n"
        "MEDIA:data:image/png;base64,aaaa\n"
    )
    result, requests = _turn([Text(reply)])

    assert len(requests) == 1
    final = result.get("final_response") or ""
    assert "https://example.com/pic.png" in final
    assert "data:image/png;base64,aaaa" in final
    assert HONEST not in final
