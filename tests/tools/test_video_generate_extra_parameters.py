"""Provider extra_parameters must reach the video_generate schema and generate().

A plugin may declare parameters outside the fixed video schema. Those
parameters belong on the served schema, and only the declared ones may be
passed through. A spec that is not a JSON-schema object is refused by name.
"""

from __future__ import annotations

from agent.video_gen_provider import VideoGenProvider
from tools import video_generation_tool as vgt


class _FakeProvider(VideoGenProvider):
    name = "extra-params-test"
    display_name = "Extra Params Test"

    def __init__(self, extra_parameters=None):
        self._extra = extra_parameters
        self.calls = []

    def capabilities(self):
        caps = {"modalities": ["text"], "max_reference_images": 0}
        if self._extra is not None:
            caps["extra_parameters"] = self._extra
        return caps

    def default_model(self):
        return "test-model"

    def list_models(self):
        return [{"id": "test-model", "modalities": ["text"]}]

    def generate(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return {"success": True, "video": "ok", "provider": self.name}

    def is_available(self):
        return True


def _select(monkeypatch, provider):
    monkeypatch.setattr(vgt, "_resolve_active_provider", lambda: provider)
    monkeypatch.setattr(vgt, "_read_configured_video_provider", lambda: provider.name)
    monkeypatch.setattr(vgt, "_read_configured_video_model", lambda: None)


def test_extra_parameter_is_on_the_schema(monkeypatch):
    _select(monkeypatch, _FakeProvider(extra_parameters={
        "continue_from": {
            "type": "string",
            "description": "Previous clip path.",
        },
    }))

    schema = vgt._build_dynamic_video_schema()
    extra = schema["parameters"]["properties"]["continue_from"]

    assert extra["type"] == "string"
    assert extra["description"] == "Previous clip path."
    assert "required" not in extra
    assert "continue_from" not in schema["parameters"]["required"]


def test_required_extra_parameter_is_required(monkeypatch):
    _select(monkeypatch, _FakeProvider(extra_parameters={
        "continue_from": {
            "type": "string",
            "description": "Previous clip path.",
            "required": True,
        },
    }))

    schema = vgt._build_dynamic_video_schema()
    extra = schema["parameters"]["properties"]["continue_from"]

    assert extra["type"] == "string"
    assert "required" not in extra
    assert "continue_from" in schema["parameters"]["required"]


def test_builtin_name_is_not_overridden(monkeypatch):
    _select(monkeypatch, _FakeProvider(extra_parameters={
        "prompt": {"type": "integer", "description": "hijack", "required": True},
        "continue_from": {"type": "string", "required": True},
    }))

    schema = vgt._build_dynamic_video_schema()
    prompt = schema["parameters"]["properties"]["prompt"]

    assert prompt["type"] == "string"
    assert "hijack" not in prompt.get("description", "")
    assert "continue_from" in schema["parameters"]["properties"]
    assert schema["parameters"]["required"].count("prompt") == 1


def test_non_dict_spec_is_refused_by_name(monkeypatch):
    _select(monkeypatch, _FakeProvider(extra_parameters={"continue_from": "not-a-schema"}))

    schema = vgt._build_dynamic_video_schema()

    assert "continue_from" not in schema["parameters"]["properties"]
    assert "continue_from" in schema["description"]
    assert "JSON-schema object" in schema["description"]


def test_non_dict_extra_parameters_map_does_not_invent_args(monkeypatch):
    _select(monkeypatch, _FakeProvider(extra_parameters="nope"))

    schema = vgt._build_dynamic_video_schema()

    assert "continue_from" not in schema["parameters"]["properties"]
    assert schema["parameters"]["required"] == ["prompt"]


def test_declared_extra_reaches_generate_and_undeclared_does_not(monkeypatch):
    provider = _FakeProvider(extra_parameters={
        "continue_from": {"type": "string"},
    })
    _select(monkeypatch, provider)

    vgt._handle_video_generate({
        "prompt": "a walk continues",
        "continue_from": "C:/renders/part1.mp4",
        "secret_knob": "nope",
    })

    assert provider.calls
    assert provider.calls[0]["continue_from"] == "C:/renders/part1.mp4"
    assert "secret_knob" not in provider.calls[0]
