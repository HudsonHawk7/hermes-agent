"""image_generate capabilities()['extra_parameters'] hook (card t_827e56a9).

Generic provider-advertised params (e.g. who=) join the dynamic schema and
reach generate() only when declared. Negative cases first.
"""
from __future__ import annotations

import copy
import json

import pytest

from agent import image_gen_registry


@pytest.fixture(autouse=True)
def _reset_registry():
    image_gen_registry._reset_for_tests()
    yield
    image_gen_registry._reset_for_tests()


def _text_only_provider(*, extra_parameters=None, name="recorder"):
    from agent.image_gen_provider import ImageGenProvider

    seen: dict = {}

    class _Prov(ImageGenProvider):
        @property
        def name(self):
            return name

        def capabilities(self):
            caps = {"modalities": ["text"], "max_reference_images": 0}
            if extra_parameters is not None:
                caps["extra_parameters"] = extra_parameters
            return caps

        def generate(self, prompt, aspect_ratio="landscape", **kwargs):
            seen.clear()
            seen.update(kwargs)
            seen["_prompt"] = prompt
            seen["_aspect_ratio"] = aspect_ratio
            return {
                "success": True,
                "image": "/tmp/recorder.png",
                "model": "m",
                "prompt": prompt,
                "aspect_ratio": aspect_ratio,
                "provider": name,
            }

        def list_models(self):
            return []

    return _Prov(), seen


class TestExtraParametersNegative:
    """AC4 / AC8 — unknown keys never reach generate; missing required is refused."""

    def test_unknown_extra_key_is_not_forwarded(self, monkeypatch, tmp_path):
        from hermes_cli import plugins as plugins_module
        from tools import image_generation_tool as ig

        who_spec = {
            "who": {
                "type": "string",
                "enum": ["none", "me", "him"],
                "description": "Who is in the picture.",
                "required": True,
            }
        }
        prov, seen = _text_only_provider(extra_parameters=who_spec)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        image_gen_registry.register_provider(prov)
        monkeypatch.setattr(ig, "_read_configured_image_provider", lambda: "recorder")
        monkeypatch.setattr(plugins_module, "_ensure_plugins_discovered", lambda **kwargs: None)

        result = json.loads(
            ig._handle_image_generate(
                {
                    "prompt": "beach",
                    "who": "me",
                    "not_a_real_extra": "sneak",
                    "creativity": "raw",
                }
            )
        )

        assert result["success"] is True
        assert seen.get("who") == "me"
        assert "not_a_real_extra" not in seen
        assert "creativity" not in seen

    def test_missing_required_extra_is_refused_and_names_the_key(self, monkeypatch, tmp_path):
        from hermes_cli import plugins as plugins_module
        from tools import image_generation_tool as ig

        who_spec = {
            "who": {
                "type": "string",
                "enum": ["none", "me", "him"],
                "description": "Who is in the picture.",
                "required": True,
            }
        }
        prov, seen = _text_only_provider(extra_parameters=who_spec)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        image_gen_registry.register_provider(prov)
        monkeypatch.setattr(ig, "_read_configured_image_provider", lambda: "recorder")
        monkeypatch.setattr(plugins_module, "_ensure_plugins_discovered", lambda **kwargs: None)

        raw = ig._handle_image_generate({"prompt": "scenery only"})
        result = json.loads(raw)

        assert "error" in result
        assert "who" in result["error"]
        assert seen == {}

    def test_none_is_a_valid_required_extra_value(self, monkeypatch, tmp_path):
        """Scenery: who=none must not be treated as missing."""
        from hermes_cli import plugins as plugins_module
        from tools import image_generation_tool as ig

        who_spec = {
            "who": {
                "type": "string",
                "enum": ["none", "me", "him"],
                "description": "Who is in the picture.",
                "required": True,
            }
        }
        prov, seen = _text_only_provider(extra_parameters=who_spec)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        image_gen_registry.register_provider(prov)
        monkeypatch.setattr(ig, "_read_configured_image_provider", lambda: "recorder")
        monkeypatch.setattr(plugins_module, "_ensure_plugins_discovered", lambda **kwargs: None)

        result = json.loads(ig._handle_image_generate({"prompt": "empty beach", "who": "none"}))
        assert result["success"] is True
        assert seen.get("who") == "none"


class TestExtraParametersSchema:
    """AC1 / AC2 / AC6 — schema shape from capabilities.extra_parameters."""

    def test_provider_without_extra_parameters_schema_matches_baseline(self, monkeypatch, tmp_path):
        from hermes_cli import plugins as plugins_module
        from tools import image_generation_tool as ig

        baseline_prov, _ = _text_only_provider(extra_parameters=None, name="plain")
        empty_prov, _ = _text_only_provider(extra_parameters={}, name="empty")

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(plugins_module, "_ensure_plugins_discovered", lambda **kwargs: None)

        image_gen_registry.register_provider(baseline_prov)
        monkeypatch.setattr(ig, "_read_configured_image_provider", lambda: "plain")
        baseline = ig._build_dynamic_image_schema()

        image_gen_registry._reset_for_tests()
        image_gen_registry.register_provider(empty_prov)
        monkeypatch.setattr(ig, "_read_configured_image_provider", lambda: "empty")
        empty = ig._build_dynamic_image_schema()

        # FAL path (no plugin) is the historical baseline when provider is unset.
        monkeypatch.setattr(ig, "_read_configured_image_provider", lambda: None)
        fal = ig._build_dynamic_image_schema()

        # No who / no extra keys on text-only plugin or empty map.
        assert "who" not in baseline["parameters"]["properties"]
        assert "who" not in empty["parameters"]["properties"]
        assert baseline["parameters"]["required"] == ["prompt"]
        assert empty["parameters"]["required"] == ["prompt"]
        # Empty map must not change the text-only plugin schema vs omit.
        assert baseline == empty
        # FAL still has upscale; text-only plugin does not — different backends, both sans who.
        assert "who" not in fal["parameters"]["properties"]

    def test_provider_advertising_who_puts_enum_and_description_on_schema(self, monkeypatch, tmp_path):
        from hermes_cli import plugins as plugins_module
        from tools import image_generation_tool as ig

        who_spec = {
            "who": {
                "type": "string",
                "enum": ["none", "me", "him"],
                "description": "Who appears in the picture.",
                "required": True,
            }
        }
        prov, _ = _text_only_provider(extra_parameters=who_spec)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        image_gen_registry.register_provider(prov)
        monkeypatch.setattr(ig, "_read_configured_image_provider", lambda: "recorder")
        monkeypatch.setattr(plugins_module, "_ensure_plugins_discovered", lambda **kwargs: None)

        schema = ig._build_dynamic_image_schema()
        props = schema["parameters"]["properties"]
        assert "who" in props
        assert props["who"]["enum"] == ["none", "me", "him"]
        assert props["who"]["description"] == "Who appears in the picture."
        assert props["who"]["type"] == "string"
        # required flag is schema-array only, never a property field.
        assert "required" not in props["who"]
        assert "who" in schema["parameters"]["required"]
        assert "prompt" in schema["parameters"]["required"]

    def test_required_false_extra_is_optional_on_schema(self, monkeypatch, tmp_path):
        from hermes_cli import plugins as plugins_module
        from tools import image_generation_tool as ig

        spec = {
            "mood": {
                "type": "string",
                "enum": ["calm", "wild"],
                "description": "Optional mood.",
                "required": False,
            }
        }
        prov, _ = _text_only_provider(extra_parameters=spec)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        image_gen_registry.register_provider(prov)
        monkeypatch.setattr(ig, "_read_configured_image_provider", lambda: "recorder")
        monkeypatch.setattr(plugins_module, "_ensure_plugins_discovered", lambda **kwargs: None)

        schema = ig._build_dynamic_image_schema()
        assert "mood" in schema["parameters"]["properties"]
        assert schema["parameters"]["required"] == ["prompt"]


class TestExtraParametersDispatch:
    """AC3 / AC7 — advertised extras reach generate()."""

    def test_who_me_reaches_generate(self, monkeypatch, tmp_path):
        from hermes_cli import plugins as plugins_module
        from tools import image_generation_tool as ig

        who_spec = {
            "who": {
                "type": "string",
                "enum": ["none", "me", "him"],
                "description": "Who is in the picture.",
                "required": True,
            }
        }
        prov, seen = _text_only_provider(extra_parameters=who_spec)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        image_gen_registry.register_provider(prov)
        monkeypatch.setattr(ig, "_read_configured_image_provider", lambda: "recorder")
        monkeypatch.setattr(plugins_module, "_ensure_plugins_discovered", lambda **kwargs: None)

        result = json.loads(ig._handle_image_generate({"prompt": "portrait", "who": "me"}))
        assert result["success"] is True
        assert seen.get("who") == "me"

    def test_schema_without_extras_is_unchanged_copy_safe(self, monkeypatch, tmp_path):
        """Regression: building schema twice with no extras stays stable (no mutation)."""
        from hermes_cli import plugins as plugins_module
        from tools import image_generation_tool as ig

        prov, _ = _text_only_provider(extra_parameters=None)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        image_gen_registry.register_provider(prov)
        monkeypatch.setattr(ig, "_read_configured_image_provider", lambda: "recorder")
        monkeypatch.setattr(plugins_module, "_ensure_plugins_discovered", lambda **kwargs: None)

        first = ig._build_dynamic_image_schema()
        snapshot = copy.deepcopy(first)
        second = ig._build_dynamic_image_schema()
        assert first == snapshot == second
