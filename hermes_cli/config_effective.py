"""The effective USER config: config.yaml + managed overlay + ``${VAR}`` expansion, no defaults.

``load_config()`` merges ``DEFAULT_CONFIG`` first, which is wrong for readers that treat a
missing key as "unset" (the gateway's presence-sensitive env bridge, ``cfg == {}`` sentinels,
cron model pinning) — so nine surfaces used to hand-roll raw-read → overlay → expand in
differing orders and none of them replayed the model-key canonicalization or the last-known-good
recovery ``load_config()`` gained. This module is that one primitive.

Order matches ``_load_config_impl``: the user layer is expanded BEFORE the managed overlay so a
managed ``${VAR}`` resolves against the process environment only (``apply_managed_overlay``
expands it) and can never be re-resolved through a profile's secret scope
(docs/design/managed-scope.md §4.1). ``read_user_config_raw`` stays the write-back primitive.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from hermes_cli import config as _config
from hermes_cli import managed_scope
from hermes_cli.config_read_errors import _warn_config_parse_failure
from utils import fast_safe_load

# path -> raw user mapping from the last successful parse in this process; served (through the
# normal pipeline) when the file is later found mid-edit as broken YAML.
_LAST_GOOD_USER_RAW: dict[str, dict[str, Any]] = {}
# path -> (*user_signature, *managed_signature, effective, env_snapshot); see utils.file_signature.
_EFFECTIVE_CACHE: dict[str, tuple[Any, ...]] = {}


def _effective(raw: dict[str, Any]) -> dict[str, Any]:
    expanded = _config._expand_env_vars(raw)
    merged = managed_scope.apply_managed_overlay(expanded if isinstance(expanded, dict) else {})
    return _config._normalize_root_model_keys(merged if isinstance(merged, dict) else {})


def _recover_user_raw(config_path: Path, path_key: str, exc: Exception) -> tuple[dict[str, Any], str]:
    """Last-known-good raw user mapping after a parse failure.

    Returns ``(raw, source)``. ``source`` is ``memory`` (this process's last
    good parse), ``backup`` (newest good copy on disk), or ``empty``.
    """
    raw = _LAST_GOOD_USER_RAW.get(path_key)
    source = "memory"
    fallback = "last-known-good"
    if raw is None:
        from hermes_cli.config_backups import load_newest_good_backup
        raw = load_newest_good_backup(config_path)
        source = "backup" if raw is not None else "empty"
        fallback = "last-known-good-backup"
    _warn_config_parse_failure(config_path, exc, fallback=fallback if raw is not None else "defaults")
    return (copy.deepcopy(raw) if raw is not None else {}), source


def _profile_model_names(config: dict[str, Any]) -> list[tuple[str, str | None]]:
    """Named profile, fallback, moa, and auxiliary models in a pre-gate copy."""
    names: list[tuple[str, str | None]] = []

    def add(model: Any, provider: Any) -> None:
        text = str(model or "").strip()
        if text:
            names.append((text, str(provider).strip() if provider else None))

    model = config.get("model")
    if isinstance(model, dict):
        add(model.get("default"), model.get("provider"))
    for item in config.get("fallback_providers") or []:
        if isinstance(item, dict):
            add(item.get("model"), item.get("provider"))
    presets = (config.get("moa") or {}).get("presets") if isinstance(config.get("moa"), dict) else None
    if isinstance(presets, dict):
        for preset in presets.values():
            if not isinstance(preset, dict):
                continue
            for slot in preset.get("reference_models") or []:
                if isinstance(slot, dict):
                    add(slot.get("model"), slot.get("provider"))
            aggregator = preset.get("aggregator")
            if isinstance(aggregator, dict):
                add(aggregator.get("model"), aggregator.get("provider"))
    auxiliary = config.get("auxiliary")
    if isinstance(auxiliary, dict):
        for block in auxiliary.values():
            if isinstance(block, dict):
                add(block.get("model"), block.get("provider"))
    return names


def _yes_restores(pre: Any) -> bool:
    """True when a GM Yes may now keep a model this copy would have skipped."""
    if not isinstance(pre, dict):
        return False
    from hermes_cli.gm_gated_models import _profile_keeps, model_is_gated

    for name, provider in _profile_model_names(pre):
        try:
            if model_is_gated(name, provider=provider, config=pre) and _profile_keeps(None, "profile", name):
                return True
        except Exception:
            return True
    return False


def _serve_profile_config(effective: dict[str, Any], *, recovered_from: str | None) -> dict[str, Any]:
    """Apply the GM model gate before a profile config is returned.

    A successful read with no named list is list-off. An in-memory last-known-good
    with no list stays list-off so a YAML typo still serves the last loaded model.
    A disk backup with no list cannot prove a model is allowed. The caller keeps
    the pre-gate copy so a later GM Yes can keep the model; this return is filtered.
    """
    served = copy.deepcopy(effective) if isinstance(effective, dict) else {}
    if recovered_from == "backup":
        from hermes_cli.gm_gated_models import filter_served_backup
        return filter_served_backup(served)
    if recovered_from == "memory":
        from hermes_cli.gm_gated_models import filter_cached_last_known_good
        return filter_cached_last_known_good(served)
    from hermes_cli.config import _apply_gm_gate_during_load
    return _apply_gm_gate_during_load(served)


def load_user_config_effective(config_path: Optional[Path] = None, *, fail_closed: bool = False) -> dict[str, Any]:
    """User ``config.yaml`` → ``${VAR}`` expansion → managed overlay → model-key canonicalization.
    NO ``DEFAULT_CONFIG`` merge: a key absent from the file (and from the managed layer) is absent
    here, so ``{}`` sentinels and presence-sensitive bridges keep working. An absent file is an
    empty user layer (the managed layer still applies). Returns a fresh deepcopy.

    Broken YAML: ``fail_closed=True`` raises the parse error (for callers that keep their own
    last-good state); otherwise the last successfully parsed user file — in-process first, then
    the newest ``backups/config/*.good.*`` copy — is served through the same pipeline, so a
    mid-edit torn write never silently drops user overrides (same contract as ``load_config``).
    A gated model is skipped on every return unless the GM answered Yes. The cache
    keeps the pre-gate copy so that Yes can restore it without a second warning
    on every cache hit. A disk backup with no list does not keep an unproven model."""
    from agent.safe_worker_policy import worker_config_snapshot

    # A frozen-policy worker is detached from the profile's files: the owner-supplied
    # snapshot IS the user layer (same seam load_config / read_raw_config honour).
    snapshot = worker_config_snapshot()
    if snapshot is not None:
        return _serve_profile_config(_effective(snapshot), recovered_from=None)
    if config_path is None:
        config_path = _config.get_config_path()
    path_key = str(config_path)
    with _config._CONFIG_LOCK:
        user_sig, cache_sig = _config._load_config_cache_sig(config_path)
        cached = _EFFECTIVE_CACHE.get(path_key)
        if cached is not None and cache_sig is not None and cached[:8] == cache_sig:
            if all(_config._env_ref_lookup(k) == v for k, v in cached[9].items()):
                pre = cached[8]
                served = cached[10] if len(cached) > 10 else None
                if isinstance(served, dict) and not _yes_restores(pre):
                    return copy.deepcopy(served)
                return _serve_profile_config(pre, recovered_from=None)

        raw: dict[str, Any] = {}
        recovered = False
        recovered_from: str | None = None
        raw_hit = _config._RAW_CONFIG_CACHE.get(path_key)
        if user_sig is not None and raw_hit is not None and raw_hit[:4] == user_sig:
            raw = copy.deepcopy(raw_hit[4])  # one parse per process, shared with read_raw_config()
            _LAST_GOOD_USER_RAW.setdefault(path_key, copy.deepcopy(raw))
        elif user_sig is not None:
            try:
                with open(config_path, encoding="utf-8-sig") as f:
                    loaded = fast_safe_load(f)
            except Exception as exc:
                if fail_closed:
                    raise
                raw, recovered_from = _recover_user_raw(config_path, path_key, exc)
                recovered = True
            else:
                raw = loaded if isinstance(loaded, dict) else {}
                _config._RAW_CONFIG_CACHE[path_key] = (*user_sig, copy.deepcopy(raw))
                _LAST_GOOD_USER_RAW[path_key] = copy.deepcopy(raw)
                # Same copy load_config keeps: a fresh process recovers from it (see _recover_user_raw).
                # Only for the ACTIVE home — a read of another profile's file (doctor, TUI cwd lookup)
                # must not create backups/ inside that profile.
                if config_path == _config.get_config_path():
                    from hermes_cli.config_backups import backup_config
                    backup_config(config_path, "good")

        env_snapshot = _config._env_ref_snapshot(raw)
        managed = managed_scope.load_managed_config()
        if managed:
            _config._env_ref_snapshot(managed, env_snapshot)
        effective = _effective(raw)
        served = _serve_profile_config(effective, recovered_from=recovered_from)
        # A recovered result is never cached under the corrupt file's signature: a later
        # ``fail_closed`` caller must still see the parse error, not a cache hit.
        # Index 8 stays the pre-gate copy so a later GM Yes can keep the model.
        if cache_sig is not None and not recovered:
            _EFFECTIVE_CACHE[path_key] = (
                *cache_sig,
                copy.deepcopy(effective),
                env_snapshot,
                copy.deepcopy(served),
            )
        else:
            _EFFECTIVE_CACHE.pop(path_key, None)
        return served
