"""Refuse test writes to the real Windows user PATH (HKCU\\Environment\\Path).

Python writers are stubbed. PowerShell ``[Environment]::SetEnvironmentVariable``
still reaches the registry, so the autouse fixture snapshots the value and
restores it if a test changes it.
"""
from __future__ import annotations

import sys

REFUSED = "TEST BUG: write to the real Windows User PATH (HKCU\\Environment)"

if sys.platform == "win32":
    import winreg

    _REAL_OPENKEY = winreg.OpenKey
    _REAL_SETVALUEEX = winreg.SetValueEx
    _REAL_DELETEVALUE = winreg.DeleteValue
    _REAL_QUERYVALUEEX = winreg.QueryValueEx
else:
    winreg = None  # type: ignore[assignment]
    _REAL_OPENKEY = _REAL_SETVALUEEX = _REAL_DELETEVALUE = _REAL_QUERYVALUEEX = None


def read_user_path() -> tuple[str, int] | None:
    """Current HKCU Path (raw string, registry type), or None if the value is absent."""
    if sys.platform != "win32":
        return None
    try:
        with _REAL_OPENKEY(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
            value, kind = _REAL_QUERYVALUEEX(key, "Path")
            return str(value), int(kind)
    except OSError:
        return None


def restore_user_path(saved: tuple[str, int] | None) -> None:
    """Write *saved* back through the real winreg, not a test wrap."""
    if sys.platform != "win32":
        return
    with _REAL_OPENKEY(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        if saved is None:
            try:
                _REAL_DELETEVALUE(key, "Path")
            except OSError:
                pass
        else:
            _REAL_SETVALUEEX(key, "Path", 0, saved[1], saved[0])
    try:
        import ctypes

        ctypes.windll.user32.SendMessageTimeoutW(  # type: ignore[attr-defined]
            0xFFFF, 0x1A, 0, "Environment", 0x0002, 5000, None
        )
    except Exception:
        pass


def refuse(*_args, **_kwargs):
    raise AssertionError(REFUSED)


def install_python_writer_stubs(monkeypatch) -> None:
    monkeypatch.setattr("hermes_cli._launchers._register_windows_user_path", refuse)
    monkeypatch.setattr("hermes_cli._install_repair._write_user_path_raw", refuse)
    monkeypatch.setattr("hermes_cli.uninstall.remove_path_from_windows_registry", refuse)
    monkeypatch.setattr("hermes_cli.uninstall._edit_user_environment", refuse)


def install_winreg_path_guard(monkeypatch) -> None:
    """Raise before in-process winreg writes to the Path value."""
    if sys.platform != "win32":
        return

    def guarded_set(key, name, reserved, typ, value):
        if str(name).casefold() == "path":
            raise AssertionError(REFUSED)
        return _REAL_SETVALUEEX(key, name, reserved, typ, value)

    def guarded_delete(key, name):
        if str(name).casefold() == "path":
            raise AssertionError(REFUSED)
        return _REAL_DELETEVALUE(key, name)

    monkeypatch.setattr(winreg, "SetValueEx", guarded_set)
    monkeypatch.setattr(winreg, "DeleteValue", guarded_delete)
