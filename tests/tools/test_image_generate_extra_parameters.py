"""Provider extra_parameters must reach the image_generate schema and generate().

A plugin may declare parameters outside the fixed creative-control list. Those
parameters belong on the served schema, and only the declared ones may be
passed through. A spec that is not a JSON-schema object is refused by name.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from agent.image_gen_provider import ImageGenProvider
from agent.image_gen_registry import get_provider, register_provider
from tools import image_generation_tool as igt
from tools.registry import registry


class _FakeProvider(ImageGenProvider):
    name = "extra-params-test"
    display_name = "Extra Params Test"

    def __init__(self, extra_parameters=None, creative_controls=None):
        self._extra = extra_parameters
        self._controls = creative_controls or []
        self.calls = []

    def capabilities(self):
        caps = {"modalities": ["text"], "max_reference_images": 0}
        if self._extra is not None:
            caps["extra_parameters"] = self._extra
        if self._controls:
            caps["creative_controls"] = list(self._controls)
        return caps

    def generate(self, prompt, aspect_ratio="landscape", **kwargs):
        self.calls.append({"prompt": prompt, "aspect_ratio": aspect_ratio, **kwargs})
        return {"success": True, "image": "ok", "provider": self.name}

    def is_available(self):
        return True


def _select(monkeypatch, provider):
    register_provider(provider)
    monkeypatch.setattr(igt, "_read_configured_image_provider", lambda: provider.name)
    monkeypatch.setattr(
        igt, "_get_plugin_provider", lambda name, force=False: get_provider(name),
    )


def test_extra_parameter_is_on_the_schema_and_required(monkeypatch):
    _select(monkeypatch, _FakeProvider(extra_parameters={
        "who": {
            "type": "string",
            "enum": ["none", "me"],
            "description": "Which face.",
            "required": True,
        },
    }))

    schema = igt._build_dynamic_image_schema()
    who = schema["parameters"]["properties"]["who"]

    assert who["enum"] == ["none", "me"]
    assert who["description"] == "Which face."
    assert "required" not in who
    assert "who" in schema["parameters"]["required"]


def test_builtin_name_is_not_overridden(monkeypatch):
    _select(monkeypatch, _FakeProvider(extra_parameters={
        "prompt": {"type": "integer", "description": "hijack", "required": True},
        "who": {"type": "string", "enum": ["none"], "required": True},
    }))

    schema = igt._build_dynamic_image_schema()
    prompt = schema["parameters"]["properties"]["prompt"]

    assert prompt["type"] == "string"
    assert "hijack" not in prompt.get("description", "")
    assert "who" in schema["parameters"]["properties"]
    assert schema["parameters"]["required"].count("prompt") == 1


def test_non_dict_spec_is_refused_by_name(monkeypatch):
    _select(monkeypatch, _FakeProvider(extra_parameters={"who": "not-a-schema"}))

    schema = igt._build_dynamic_image_schema()

    assert "who" not in schema["parameters"]["properties"]
    assert "who" in schema["description"]
    assert "JSON-schema object" in schema["description"]

    entry = registry.get_entry("image_generate")
    monkeypatch.setattr(entry, "check_fn", lambda: True)
    served = registry.get_definitions({"image_generate"}, quiet=True)
    assert served
    assert "who" in served[0]["function"]["description"]
    assert "who" not in served[0]["function"]["parameters"]["properties"]


def test_non_dict_extra_parameters_map_does_not_invent_args(monkeypatch):
    _select(monkeypatch, _FakeProvider(extra_parameters="nope"))

    schema = igt._build_dynamic_image_schema()

    assert "who" not in schema["parameters"]["properties"]
    assert schema["parameters"]["required"] == ["prompt"]


def test_declared_extra_reaches_generate_and_undeclared_does_not(monkeypatch):
    provider = _FakeProvider(extra_parameters={
        "who": {"type": "string", "enum": ["none", "me"], "required": True},
    })
    _select(monkeypatch, provider)

    igt._handle_image_generate({
        "prompt": "a portrait",
        "who": "me",
        "secret_knob": "nope",
    })

    assert provider.calls
    assert provider.calls[0]["who"] == "me"
    assert "secret_knob" not in provider.calls[0]


def test_creative_control_still_passes_beside_an_extra(monkeypatch):
    provider = _FakeProvider(
        extra_parameters={"who": {"type": "string", "enum": ["none"]}},
        creative_controls=["resolution"],
    )
    _select(monkeypatch, provider)

    igt._handle_image_generate({
        "prompt": "a portrait",
        "who": "none",
        "resolution": "1024",
        "creativity": 9,
    })

    assert provider.calls[0]["who"] == "none"
    assert provider.calls[0]["resolution"] == "1024"
    assert "creativity" not in provider.calls[0]


def _force_media_root() -> Path | None:
    raw = os.environ.get("HERMES_FORCE_MEDIA", "").strip()
    if not raw:
        return None
    root = Path(raw)
    plugin = root / "image_gen" / "sogni" / "__init__.py"
    return root if plugin.is_file() else None


def _stage_installed_sogni(root: Path, dest: Path) -> Path:
    """The installed shape: plugin folder with force_media_core copied inside it."""
    plugin = dest / "sogni"
    shutil.copytree(root / "image_gen" / "sogni", plugin, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(root / "force_media_core", plugin / "force_media_core", ignore=shutil.ignore_patterns("__pycache__"))
    return plugin


def _load_sogni_plugin(plugin_dir: Path):
    """Load the staged plugin the way Hermes loads a directory plugin."""
    from hermes_cli.plugins import get_plugin_manager
    from hermes_cli.plugins_manifest import PluginManifest

    manifest = PluginManifest(
        name="sogni",
        kind="backend",
        key="image_gen/sogni-schema-probe",
        path=str(plugin_dir),
        source="user",
    )
    return get_plugin_manager()._load_directory_module(manifest)


def test_real_sogni_plugin_cast_root_puts_who_on_served_schema(monkeypatch, tmp_path):
    """The proof the earlier spikes lacked: the real plugin, through Hermes' schema."""
    root = _force_media_root()
    if root is None:
        pytest.skip("HERMES_FORCE_MEDIA is not set to the Force media plugin tree")

    plugin = _load_sogni_plugin(_stage_installed_sogni(root, tmp_path / "installed"))
    roster = plugin.force_media_core.cast_roster
    character_id = roster.IN_SCOPE_IDS[0]
    face = tmp_path / character_id / "1.png"
    face.parent.mkdir()
    face.write_bytes(b"png")

    cfg = {
        "image_gen": {
            "provider": "sogni",
            "sogni": {"cast_root": str(tmp_path)},
        }
    }
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: cfg)
    # The plugin binds the function at import. Patch that binding, not only the source module.
    monkeypatch.setattr(plugin, "load_config_readonly", lambda: cfg)

    provider = plugin.SogniImageGenProvider()
    register_provider(provider)
    monkeypatch.setattr(igt, "_read_configured_image_provider", lambda: "sogni")
    monkeypatch.setattr(
        igt, "_get_plugin_provider", lambda name, force=False: get_provider(name),
    )

    entry = registry.get_entry("image_generate")
    monkeypatch.setattr(entry, "check_fn", lambda: True)
    served = registry.get_definitions({"image_generate"}, quiet=True)
    assert served, "image_generate was not served"
    function = served[0]["function"]
    props = function["parameters"]["properties"]
    required = function["parameters"]["required"]

    assert "who" in props
    assert "who" in required
    declared = provider.capabilities()["extra_parameters"]["who"]
    assert props["who"]["enum"] == declared["enum"]
    assert "required" not in props["who"]
