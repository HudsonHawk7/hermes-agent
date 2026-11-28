"""Pure-data factory for auxiliary model configuration defaults."""


def _aux(timeout, *, reasoning_effort=True, **extra):
    """Standard auxiliary-task model block (see DEFAULT_CONFIG["auxiliary"]).

    reasoning_effort=False omits that key (MoA blocks configure depth per slot);
    ``extra`` keys are appended after the standard ones.
    """
    # allow_main_fallback: false skips the main-agent last resort so a cloud-pinned
    # task cannot land on the local GPU serving the chat. Missing in a saved config
    # still means true (the reader treats absence as today's fallback).
    d = {
        "provider": "auto", "model": "", "base_url": "", "api_key": "",
        "timeout": timeout, "extra_body": {}, "allow_main_fallback": True,
    }
    if reasoning_effort:
        d["reasoning_effort"] = ""
    d.update(extra)
    return d
