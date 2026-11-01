"""The file half of ``drive_preview`` action='upload': which local file may be handed to the page open in the
desktop preview pane, and how it travels.

A path arrives from the model, so it is untrusted text: it is judged before any filesystem call (a link, a network
or device path and a relative path are refused unseen — on Windows even resolving ``\\\\host\\share`` opens an SMB
connection), then resolved with symlinks and junctions followed and judged again where it really leads. The
allowed folders are only the ones the running profile configured for its picture and video providers, plus the
profile's own retained chat attachments; none is defaulted. The bytes travel to the renderer as base64, so the
renderer never touches a path.
"""

import base64
import os
import re
import stat
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_dir

#: One bridge frame carries the file as base64 (x4/3) and the guest page decodes it in memory, so the cap keeps one
#: upload to a few tens of MB on the wire: a phone photo or a PNG screenshot is under 10 MB, a short clip fits, and
#: the gateway's blocking bridge is never held on a multi-hundred-MB transfer.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

#: Extension -> (MIME type, the header family its bytes must show). ``MP4`` is the ISO BMFF ``ftyp`` box shared by
#: .mp4/.m4v/.mov; ``WebM`` is the EBML header.
UPLOAD_TYPES = {
    ".png": ("image/png", "PNG"),
    ".jpg": ("image/jpeg", "JPEG"),
    ".jpeg": ("image/jpeg", "JPEG"),
    ".webp": ("image/webp", "WebP"),
    ".gif": ("image/gif", "GIF"),
    ".mp4": ("video/mp4", "MP4"),
    ".m4v": ("video/mp4", "MP4"),
    ".mov": ("video/quicktime", "MP4"),
    ".webm": ("video/webm", "WebM"),
}

#: ``(section, key)`` read under every provider of the section: ``image_gen.<provider>.image_input_dir``.
#: A provider's ``screenshots_dir`` is deliberately absent: it holds live screen-share frames (work screens,
#: customer data), which the provider alone may read; ``upload`` hands a file to any site open in the pane.
PROVIDER_FOLDER_KEYS = (("image_gen", "image_input_dir"), ("video_gen", "image_input_dir"))

#: The profile's retained chat attachments (gateway/session_ingress_media.py): ``native-inputs`` under the documents
#: cache, which an older home keeps at ``document_cache/`` — ``hermes_constants.get_hermes_dir`` is the one rule for
#: that, the same one ``gateway.platforms.base.get_document_cache_dir`` applies before it creates the folder.
DOCUMENT_CACHE = ("cache/documents", "document_cache")
NATIVE_INPUTS = "native-inputs"

SETTINGS_HINT = "image_gen.<provider>.image_input_dir or video_gen.<provider>.image_input_dir"

# A URL scheme has two or more letters; a Windows drive letter has one.
_LINK = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]+:")


class UploadRefused(ValueError):
    """The path may not be handed to the page; the message says why and what would be accepted."""


def upload_roots(cfg: Any, hermes_home: Path) -> list[tuple[str, Path]]:
    """``[(setting, folder)]`` a page may be given a file from: every configured provider folder, then the
    profile's retained attachments once that folder exists (its first attachment creates it)."""
    roots: list[tuple[str, Path]] = []
    for section, key in PROVIDER_FOLDER_KEYS:
        providers = cfg.get(section) if isinstance(cfg, dict) else None
        if not isinstance(providers, dict):
            continue
        for provider, settings in providers.items():
            value = settings.get(key) if isinstance(settings, dict) else None
            if isinstance(value, str) and value.strip():
                roots.append((f"{section}.{provider}.{key}", Path(value.strip()).expanduser()))
    home = Path(hermes_home)
    native = get_hermes_dir(*DOCUMENT_CACHE, home=home) / NATIVE_INPUTS
    if native.is_dir():
        roots.append((native.relative_to(home).as_posix(), native))
    return roots


def configured_roots() -> list[tuple[str, Path]]:
    """The running profile's allowed folders."""
    from hermes_cli.config import load_config
    from hermes_constants import get_hermes_home

    return upload_roots(load_config(), get_hermes_home())


def _normal(path: Any) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _inside(candidate: str, folder: Any) -> bool:
    return candidate.startswith(_normal(folder).rstrip("\\/") + os.sep)


def _allowed_folders(roots: list[tuple[str, Path]]) -> str:
    return "; ".join(f"{name} ({os.path.normpath(folder)})" for name, folder in roots)


def _refuse_outside(text: str, roots: list[tuple[str, Path]]) -> None:
    raise UploadRefused(f"{text} is outside the folders a page may be given a file from: {_allowed_folders(roots)}.")


def _mb(size: int) -> str:
    return f"{size / (1024 * 1024):.1f} MB"


def _reason(exc: BaseException) -> str:
    return getattr(exc, "strerror", None) or str(exc)


def _header_family(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if head.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WebP"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF"
    if head[4:8] == b"ftyp":
        return "MP4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "WebM"
    return None


def read_upload(path_text: str | None, roots: list[tuple[str, Path]]) -> dict[str, Any]:
    """The checked file as the renderer takes it: ``{name, mime, size, data}`` with ``data`` base64. Raises
    ``UploadRefused`` with the reason and the allowed folders for anything else."""
    if not roots:
        raise UploadRefused("No folder is configured for uploads, so no file can be handed to the page. "
                            f"Set {SETTINGS_HINT} to the folder the files live in.")
    text = (path_text or "").strip()
    if not text:
        raise UploadRefused("upload needs the path of one picture or video file.")
    if "\x00" in text:
        raise UploadRefused("the path contains a NUL character, which no file name can.")
    if text.replace("/", "\\").startswith("\\\\"):
        raise UploadRefused("network and device paths (\\\\host\\share, //host/share, \\\\?\\, \\\\.\\) "
                            "cannot be uploaded.")
    if _LINK.match(text):
        raise UploadRefused(f"{text} is a link, not a local file; give the path of a file inside "
                            f"{_allowed_folders(roots)}.")
    path = Path(text)
    if not path.is_absolute():
        raise UploadRefused(f"{text} is not a full path; give the whole path of a file inside "
                            f"{_allowed_folders(roots)}.")
    kind = UPLOAD_TYPES.get(path.suffix.lower())
    if kind is None:
        raise UploadRefused(f"only {', '.join(UPLOAD_TYPES)} files can be uploaded, "
                            f"not {path.suffix or 'a file with no extension'}.")
    if not any(_inside(_normal(text), folder) for _, folder in roots):
        _refuse_outside(text, roots)
    try:
        source = path.resolve(strict=True)
    except FileNotFoundError:
        raise UploadRefused(f"There is no file at {text}.") from None
    except (OSError, ValueError) as exc:
        raise UploadRefused(f"{text} cannot be read: {_reason(exc)}.") from None
    if not any(_inside(_normal(source), Path(folder).resolve()) for _, folder in roots):
        _refuse_outside(text, roots)
    if UPLOAD_TYPES.get(source.suffix.lower()) != kind:
        raise UploadRefused(f"{text} leads to {source.name}, which is not the same kind of file.")
    try:
        status = source.stat()
    except OSError as exc:
        raise UploadRefused(f"{text} cannot be read: {_reason(exc)}.") from None
    if not stat.S_ISREG(status.st_mode):
        raise UploadRefused(f"{text} is not a file.")
    if status.st_size > MAX_UPLOAD_BYTES:
        raise UploadRefused(f"{source.name} is {_mb(status.st_size)}, larger than the "
                            f"{_mb(MAX_UPLOAD_BYTES)} an upload may be.")
    try:
        with open(source, "rb") as handle:
            data = handle.read(MAX_UPLOAD_BYTES + 1)
    except OSError as exc:
        raise UploadRefused(f"{text} cannot be read: {_reason(exc)}.") from None
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadRefused(f"{source.name} read more than the {_mb(MAX_UPLOAD_BYTES)} an upload may be; "
                            "it changed while being read.")
    mime, family = kind
    found = _header_family(data[:12])
    if found != family:
        raise UploadRefused(f"{source.name} has a {source.suffix} extension but its bytes are "
                            f"{found or 'not a known picture or video'}, not a {family}.")
    return {"name": source.name, "mime": mime, "size": len(data), "data": base64.b64encode(data).decode("ascii")}
