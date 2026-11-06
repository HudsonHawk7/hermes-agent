"""A local Ollama route uses the served window, not the GGUF model card."""
from agent.model_metadata import (
    _query_local_context_length_uncached,
    local_ollama_served_context,
)

_SHOW_CARD = {
    "model_info": {"qwen3.context_length": 262144},
    "parameters": "",
}
_PS_SERVED = {"models": [{"name": "qwen3.8:27b", "context_length": 65536}]}


def test_served_window_beats_the_model_card():
    assert local_ollama_served_context(_SHOW_CARD, _PS_SERVED, "qwen3.8:27b") == 65536


def test_num_ctx_is_used_when_the_model_is_not_loaded():
    show = {"model_info": {"qwen3.context_length": 262144}, "parameters": "num_ctx 65536"}
    assert local_ollama_served_context(show, {"models": []}, "qwen3.8:27b") == 65536


def test_gguf_card_alone_is_not_the_served_window():
    assert local_ollama_served_context(_SHOW_CARD, {"models": []}, "qwen3.8:27b") is None


def test_partial_gpu_fit_is_said_in_plain_words():
    from agent.model_metadata import model_fit_warning_from_ps
    partial = {"models": [{"name": "solara:gemma4-31b-uncensored", "size": 100, "size_vram": 10}]}
    full = {"models": [{"name": "qwen3.8:27b", "size": 100, "size_vram": 100}]}
    warning = model_fit_warning_from_ps("solara:gemma4-31b-uncensored", partial)
    assert warning == "solara:gemma4-31b-uncensored doesn't fully fit on the card with pictures; replies can take minutes"
    assert model_fit_warning_from_ps("qwen3.8:27b", full) is None
    assert model_fit_warning_from_ps("solara:gemma4-31b-uncensored", partial) != "qwen3.8:27b"


class _Resp:
    def __init__(self, code, body):
        self.status_code = code
        self._body = body

    def json(self):
        return self._body


class _Client:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, json=None):
        return _Resp(200, _SHOW_CARD)

    def get(self, url):
        if str(url).endswith("/api/ps"):
            return _Resp(200, _PS_SERVED)
        if str(url).endswith("/v1/models"):
            return _Resp(200, {"data": [{"id": "qwen3.8:27b", "context_length": 262144}]})
        return _Resp(200, {"context_length": 262144})


def test_local_probe_returns_the_served_window(monkeypatch):
    monkeypatch.setattr("agent.model_metadata.detect_local_server_type", lambda *a, **k: "ollama")
    monkeypatch.setattr("httpx.Client", _Client)
    got = _query_local_context_length_uncached(
        "qwen3.8:27b", "http://127.0.0.1:11434/v1", api_key=None,
    )
    assert got == 65536


class _CardOnlyClient(_Client):
    """No served window and no num_ctx. The card is the only number the server offers."""

    def get(self, url):
        if str(url).endswith("/api/ps"):
            return _Resp(200, {"models": []})
        return _Resp(200, {"id": "qwen3.8:27b", "context_length": 262144, "data": [{"id": "qwen3.8:27b", "context_length": 262144}]})


def test_ollama_probe_does_not_fall_through_to_the_gguf_card(monkeypatch):
    monkeypatch.setattr("agent.model_metadata.detect_local_server_type", lambda *a, **k: "ollama")
    monkeypatch.setattr("httpx.Client", _CardOnlyClient)
    got = _query_local_context_length_uncached(
        "qwen3.8:27b", "http://127.0.0.1:11434/v1", api_key=None,
    )
    assert got is None
    assert got != 262144
