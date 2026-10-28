"""``hermes voice`` subcommand parser (VH5 speaker ID).

Kept deliberately small: the three commands that own the whole feature lifecycle — install the
model once, enrol a voice, read the gate's state — plus ``verify`` for replaying wav files through
the production gate. Nothing here arms the microphone or touches another profile.
"""

from __future__ import annotations


def build_voice_parser(subparsers) -> None:
    """Attach the ``voice`` subcommand group to ``subparsers``."""
    voice = subparsers.add_parser(
        "voice",
        help="Speaker ID for the listening guard: enrol a voice, install the model, check state",
        description=(
            "Who is speaking. The listening guard decides THAT someone spoke; `hermes voice` "
            "decides WHO, locally: an ONNX embedding model on this disk, an enrolment file on "
            "this disk, and a threshold you set. Nothing is uploaded and no audio is kept."),
    )
    voice_sub = voice.add_subparsers(dest="voice_command", metavar="<command>")

    setup = voice_sub.add_parser(
        "setup", help="Install the speaker-ID model (29.6 MB, sha256 verified)")
    setup.add_argument("--install-speaker-model", action="store_true",
                       help="fetch the model once into <data root>/models/speaker/")
    setup.add_argument("--from-file", metavar="PATH", default=None,
                       help="copy a local model file instead of downloading (still hash-checked)")

    enrol = voice_sub.add_parser("enrol", help="Record ~30 s of a voice and build its enrolment")
    enrol.add_argument("--identity", metavar="NAME", default=None,
                       help="the name this voice is recognised as (e.g. judson)")
    enrol.add_argument("--threshold", metavar="COSINE", type=float, default=None,
                       help="utterance match threshold; the VH4 measurement recommends 0.62")
    enrol.add_argument("--seconds", metavar="S", type=float, default=None,
                       help="total seconds of speech to record (default 30)")
    enrol.add_argument("--sentences", metavar="N", type=int, default=None,
                       help="how many separate recordings to split it into (default 10)")
    enrol.add_argument("--from-wav", metavar="PATH", action="append", default=None,
                       help="enrol from wav files instead of the microphone (repeatable)")
    enrol.add_argument("--enrolment-dir", metavar="DIR", default=None,
                       help="where to write <identity>.enrol.json")
    enrol.add_argument("--enable", action="store_true",
                       help="write the voice.speaker.* settings that turn the gate on")

    status = voice_sub.add_parser(
        "status", help="What the gate would do now, and the exact reason when it would refuse")
    status.add_argument("--json", action="store_true", help="machine-readable output")

    verify = voice_sub.add_parser(
        "verify", help="Score wav files through the production gate (one row each)")
    verify.add_argument("--genuine", metavar="PATH", action="append", default=None,
                        help="a wav of the enrolled voice (repeatable)")
    verify.add_argument("--impostor", metavar="PATH", action="append", default=None,
                        help="a wav of anyone else (repeatable)")
    verify.add_argument("--json", action="store_true", help="machine-readable output")

    def _dispatch_voice(args):
        # `hermes voice` with no command prints the group's help instead of arming anything.
        if not getattr(args, "voice_command", None):
            voice.print_help()
            return 0
        from hermes_cli.voice_speaker import run_voice_command

        code = run_voice_command(args)
        return 0 if code is None else code

    voice.set_defaults(func=_dispatch_voice)
