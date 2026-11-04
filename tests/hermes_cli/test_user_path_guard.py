"""Default tests must not write the real Windows user PATH."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.platforms("windows")


def test_python_user_path_writers_are_refused():
    from hermes_cli._install_repair import _write_user_path_raw
    from hermes_cli._launchers import _register_windows_user_path
    from hermes_cli.uninstall import _edit_user_environment, remove_path_from_windows_registry

    with pytest.raises(AssertionError, match="HKCU"):
        _register_windows_user_path(Path("C:/nope"))
    with pytest.raises(AssertionError, match="HKCU"):
        _write_user_path_raw(["C:/nope"], 1)
    with pytest.raises(AssertionError, match="HKCU"):
        remove_path_from_windows_registry(Path("C:/nope"))
    with pytest.raises(AssertionError, match="HKCU"):
        _edit_user_environment(lambda *_a, **_k: None, warn_label="test")


def test_winreg_setvalueex_path_is_refused():
    import winreg

    from tests.user_path_guard import read_user_path

    before = read_user_path()
    with pytest.raises(AssertionError, match="HKCU"):
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "Path", 0, winreg.REG_SZ, before[0] if before else "")
    assert read_user_path() == before
