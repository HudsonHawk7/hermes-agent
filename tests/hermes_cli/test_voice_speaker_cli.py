"""``hermes voice`` — the command surface the GM actually types.

Numpy-free: these tests exercise routing, the parser, the refusal report and the config write. The
model and the audio live behind ``tools.voice_speaker`` and are covered by its own tests.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.config import get_config_path
from hermes_cli.subcommands.voice import build_voice_parser
from hermes_cli.voice_speaker import apply_speaker_config, run_voice_command


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_voice_parser(subparsers)
    return parser.parse_args(argv)


def write_config(text: str) -> Path:
    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ── the parser ──────────────────────────────────────────────────────────────────────────────
def test_the_exact_command_line_the_card_gives_the_gm_parses():
    args = parse(["voice", "enrol", "--identity", "judson", "--threshold", "0.62", "--enable"])
    assert args.command == "voice"
    assert args.voice_command == "enrol"
    assert args.identity == "judson"
    assert args.threshold == pytest.approx(0.62)
    assert args.enable is True


def test_setup_status_and_verify_parse_with_their_own_options():
    setup = parse(["voice", "setup", "--install-speaker-model"])
    assert setup.voice_command == "setup" and setup.install_speaker_model is True
    status = parse(["voice", "status", "--json"])
    assert status.voice_command == "status" and status.json is True
    verify = parse(["voice", "verify", "--genuine", "a.wav", "--impostor", "b.wav"])
    assert verify.genuine == ["a.wav"] and verify.impostor == ["b.wav"]


def test_no_subcommand_is_a_help_request_not_a_crash():
    args = parse(["voice"])
    assert args.voice_command is None
    assert run_voice_command(args) is None


def test_an_unknown_subcommand_returns_none():
    assert run_voice_command(SimpleNamespace(voice_command="nonsense")) is None


# ── enrol: refuses before it records anything ───────────────────────────────────────────────
def test_enrol_without_an_identity_says_so_and_does_nothing(capsys):
    code = run_voice_command(SimpleNamespace(voice_command="enrol", identity="  "))
    assert code == 1
    assert "--identity is required" in capsys.readouterr().out


def test_enrol_without_a_threshold_quotes_the_measured_one(capsys):
    code = run_voice_command(SimpleNamespace(voice_command="enrol", identity="judson",
                                             threshold=None))
    out = capsys.readouterr().out
    assert code == 1
    assert "--threshold 0.62" in out
    assert "FAR 0/164" in out


# ── status: off, and the named refusal ──────────────────────────────────────────────────────
def test_status_says_off_and_what_to_do_about_it(capsys):
    write_config("model: test-model\n")
    code = run_voice_command(SimpleNamespace(voice_command="status", json=False))
    out = capsys.readouterr().out
    assert code == 0
    assert "Speaker ID: off" in out
    assert "hermes voice enrol --identity" in out


def test_status_names_the_refusal_instead_of_guessing(capsys):
    # A clean HOME has no model, and the model is checked before the enrolment: the first named
    # refusal is the one worth reporting. (AC4 covers the enrolment case with a real model.)
    write_config(
        "model: test-model\n"
        "voice:\n"
        "  speaker:\n"
        "    enabled: true\n"
        "    match_threshold: 0.62\n"
        "    enrolment_dir: null\n"
        "    trusted_identities: [judson]\n"
    )
    code = run_voice_command(SimpleNamespace(voice_command="status", json=False))
    out = capsys.readouterr().out
    assert code == 1
    assert "REFUSES TO LISTEN — model_missing" in out
    assert "no speaker model at" in out  # the detail line names the exact path it wanted
    assert "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx" in out


def test_status_json_is_what_a_script_would_read(capsys):
    write_config("model: test-model\n")
    assert run_voice_command(SimpleNamespace(voice_command="status", json=True)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "off"
    assert report["settings"]["enabled"] is False
    assert report["settings"]["match_threshold"] is None
    assert report["model"].endswith(".onnx")


# ── the config write ───────────────────────────────────────────────────────────────────────
def test_enrolment_writes_only_the_keys_this_feature_owns(tmp_path):
    write_config(
        "model: test-model\n"
        "voice:\n"
        "  wake_word:\n"
        "    enabled: false\n"
        "  speaker:\n"
        "    enabled: false\n"
        "    trusted_identities: [someone-else]\n"
    )
    path = apply_speaker_config(enrolment_dir=str(tmp_path / "speaker"), threshold=0.62,
                                identity="judson")
    text = Path(path).read_text(encoding="utf-8")
    assert "model: test-model" in text
    assert "wake_word" in text
    assert "someone-else" in text  # an existing trusted identity is kept, not replaced
    assert "judson" in text
    assert "0.62" in text
    assert "[someone-else, judson]" in text or "someone-else" in text
