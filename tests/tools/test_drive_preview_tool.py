"""Tests for the GUI-surface ``drive_preview`` tool."""

import base64
import json

import pytest

from tools import drive_preview_tool as ap




def test_requires_callback():
    """Outside the desktop GUI there is no bridge — a clear error, no crash."""
    result = json.loads(ap.drive_preview_tool(action="elements", callback=None))
    assert "desktop" in result["error"]


def test_rejects_an_unknown_action():
    result = json.loads(ap.drive_preview_tool(action="teleport", callback=lambda _p: "{}"))
    assert "action must be one of" in result["error"]


def test_interaction_verbs_need_a_target():
    """A click with nowhere to land is a mistake worth naming before the bridge."""
    for verb in ("click", "type", "press"):
        result = json.loads(ap.drive_preview_tool(action=verb, text="x", key="Enter", callback=lambda _p: "{}"))
        assert "ref" in result["error"], verb


def test_type_needs_text_and_press_needs_a_key():
    calls = []

    def cb(payload):
        calls.append(payload)
        return json.dumps({"success": True})

    assert "text" in json.loads(ap.drive_preview_tool(action="type", ref="@e1", callback=cb))["error"]
    assert "key" in json.loads(ap.drive_preview_tool(action="press", ref="@e1", callback=cb))["error"]
    assert calls == []


def test_typing_an_empty_string_is_allowed():
    """Clearing a field is a real intent — only a missing `text` is an error."""
    seen = {}
    ap.drive_preview_tool(action="type", ref="@e1", text="", callback=lambda p: seen.update(p) or "{}")

    assert seen["text"] == ""


def test_scroll_needs_no_target_and_validates_its_destination():
    seen = {}

    def cb(payload):
        seen.clear()
        seen.update(payload)
        return json.dumps({"success": True})

    ap.drive_preview_tool(action="scroll", callback=cb)
    assert seen == {"action": "scroll"}

    result = json.loads(ap.drive_preview_tool(action="scroll", to="sideways", callback=cb))
    assert "to must be one of" in result["error"]


def test_payload_forwards_only_what_was_given():
    seen = {}
    ap.drive_preview_tool(
        action="type",
        ref="inp-password",
        text="hunter2",
        submit=True,
        callback=lambda p: seen.update(p) or json.dumps({"success": True}),
    )

    assert seen == {"action": "type", "ref": "inp-password", "text": "hunter2", "submit": True}




def test_numeric_arguments_are_validated():
    result = json.loads(ap.drive_preview_tool(action="scroll", amount="lots", callback=lambda _p: "{}"))
    assert "integers" in result["error"]


def test_empty_answer_means_nothing_open():
    result = json.loads(ap.drive_preview_tool(action="elements", callback=lambda _p: ""))
    assert "open_preview" in result["error"]


def test_passes_the_renderer_answer_through():
    payload = {
        "success": True,
        "acted": 'clicked button "Sign in"',
        "url": "https://example.com/app",
        "elements": [{"ref": "@e1", "role": "button", "label": "Log out", "selector": "#out"}],
    }
    result = json.loads(ap.drive_preview_tool(action="click", ref="@e2", callback=lambda _p: json.dumps(payload)))

    assert result == payload


def test_wraps_non_json_text():
    result = json.loads(ap.drive_preview_tool(action="elements", callback=lambda _p: "plain words"))
    assert result == {"text": "plain words"}


def test_callback_failure_is_reported():
    def _boom(_payload):
        raise RuntimeError("renderer went away")

    result = json.loads(ap.drive_preview_tool(action="elements", callback=_boom))
    assert "renderer went away" in result["error"]


def test_empty_answer_distinguishes_no_tab_from_a_stale_app():
    """An empty bridge answer used to be one merged "timed out, or no window
    answered" string that blamed a closed tab even when the pane was open on an
    app older than this backend (#94272): the two cases need different next
    steps (open a tab vs update the app)."""
    result = json.loads(ap.drive_preview_tool(action="elements", callback=lambda _p: ""))

    assert "no preview tab is open" in result["error"]
    assert "older than this backend" in result["error"]


# ── upload: hand the page an allowed local picture or video ─────────────────

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


@pytest.fixture
def allowed(tmp_path, monkeypatch):
    """One allowed folder, as the running profile would configure it, with a PNG inside."""
    from tools import preview_upload

    folder = tmp_path / "pictures"
    folder.mkdir()
    (folder / "judson.png").write_bytes(PNG)
    monkeypatch.setattr(preview_upload, "configured_roots", lambda: [("image_gen.sogni.image_input_dir", folder)])
    return folder


def test_upload_needs_a_target_and_a_path(allowed):
    calls = []

    def cb(payload):
        calls.append(payload)
        return "{}"

    assert "ref" in json.loads(ap.drive_preview_tool(action="upload", path=str(allowed / "judson.png"), callback=cb))["error"]
    assert "path" in json.loads(ap.drive_preview_tool(action="upload", ref="inp-photo", callback=cb))["error"]
    assert calls == []


def test_upload_outside_the_allowed_folders_never_reaches_the_renderer(allowed, tmp_path):
    outside = tmp_path / "secret.png"
    outside.write_bytes(PNG)
    calls = []

    result = json.loads(ap.drive_preview_tool(
        action="upload", ref="inp-photo", path=str(outside), callback=lambda p: calls.append(p) or "{}"))

    assert str(allowed) in result["error"]
    assert "image_gen.sogni.image_input_dir" in result["error"]
    assert calls == []


def test_upload_sends_the_checked_file_over_the_bridge(allowed):
    seen = {}
    answer = {"success": True, "acted": 'uploaded judson.png (56 B) into input "Photo"'}

    result = json.loads(ap.drive_preview_tool(
        action="upload", ref="inp-photo", path=str(allowed / "judson.png"),
        callback=lambda p: seen.update(p) or json.dumps(answer)))

    assert seen == {
        "action": "upload",
        "ref": "inp-photo",
        "file": {"name": "judson.png", "mime": "image/png", "size": len(PNG), "data": base64.b64encode(PNG).decode()},
    }
    assert result == answer


def test_upload_with_no_configured_folder_names_the_setting(monkeypatch):
    from tools import preview_upload

    monkeypatch.setattr(preview_upload, "configured_roots", list)
    calls = []

    result = json.loads(ap.drive_preview_tool(
        action="upload", selector="input[type=file]", path="C:/anywhere/judson.png",
        callback=lambda p: calls.append(p) or "{}"))

    assert "image_gen.<provider>.image_input_dir" in result["error"]
    assert calls == []


def test_upload_is_offered_to_the_model():
    schema = ap.ACT_PREVIEW_SCHEMA
    assert "upload" in schema["parameters"]["properties"]["action"]["enum"]
    assert "path" in schema["parameters"]["properties"]
    assert "upload" in schema["description"]
