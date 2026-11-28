"""Desktop preview-signins must reach the shared-profile lock dir without a missing import.

The 21:05 boot failed with ``ImportError: cannot import name '_get_lock_dir' from
'gateway.status'``. The desktop child runs ``hermes_cli.browser_preview_signins``;
a persistent profile launch asks ``_shared_profiles_root`` for the same lock
directory the gateway's own cross-profile locks use.
"""

from __future__ import annotations


def test_preview_signins_shared_profile_root_uses_the_machine_lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path))
    import hermes_cli.browser_preview_signins  # the CLI the desktop child runs
    from tools.browser_launcher import _shared_profiles_root

    assert _shared_profiles_root() == tmp_path / "browser-shared-profiles"
