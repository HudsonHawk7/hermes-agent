"""The inline memory alias persists writes and preserves content precedence."""

import json
from types import SimpleNamespace

from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
from tools.memory_tool_store import MemoryStore


def test_memory_alias_persists_with_content_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = MemoryStore(memory_char_limit=500, user_char_limit=300)
    from agent.memory_manager import MemoryManager

    class Sink:
        name = "test-sink"
        writes = []

        def get_tool_schemas(self):
            return []

        def on_memory_write(self, action, target, content, metadata=None):
            self.writes.append((action, target, content))

    sink = Sink()
    manager = MemoryManager()
    manager.add_provider(sink)
    agent = SimpleNamespace(_memory_store=store, _memory_manager=manager,
                            _build_memory_write_metadata=lambda **kwargs: kwargs)
    ctx = InlineToolContext(effective_task_id="task-1", tool_call_id="call-1")

    def call(**args):
        return json.loads(INLINE_TOOL_EXECUTORS["memory"](agent, args, ctx))

    assert call(action="add", target="user", content="Household owns an estate.")["success"]
    result = call(action="patch", target="user", old_text="Household owns an estate.",
                  new_text="Household owns a saloon.")
    assert result["success"], result
    path = tmp_path / "memories" / "USER.md"
    assert "Household owns a saloon." in path.read_text(encoding="utf-8")
    assert sink.writes[-1] == ("replace", "user", "Household owns a saloon.")
    result = call(action="patch", target="user", old_text="Household owns a saloon.",
                  content="Household owns a hatchback.", new_text="Household owns a van.")
    assert result["success"], result
    persisted = path.read_text(encoding="utf-8")
    assert "Household owns a hatchback." in persisted
    assert "van" not in persisted
    assert "saloon" not in persisted
    assert sink.writes[-1] == ("replace", "user", "Household owns a hatchback.")
    result = call(target="user", operations=[{"action": "patch", "old_text": "Household owns a hatchback.",
                                             "new_text": "Household owns a coupe."}])
    assert result["success"], result
    assert sink.writes[-1] == ("replace", "user", "Household owns a coupe.")


def test_broken_operations_json_string_returns_tool_error(tmp_path, monkeypatch):
    """A broken operations JSON string must not crash the tool round.

    The agent path (inline executor) coerces that string into a one-item list.
    The handler used to call .get on the string, so the turn died with no tool_result.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # The agent has the memory schema registered before coerce runs. Without it,
    # coerce leaves the broken string alone and the list guard hides the crash.
    import tools.memory_tool
    store = MemoryStore(memory_char_limit=500, user_char_limit=300)
    agent = SimpleNamespace(_memory_store=store, _memory_manager=None)
    ctx = InlineToolContext(effective_task_id="task-1", tool_call_id="call-broken")
    # Missing comma: the same parse failure the live log named ("Expecting ',' delimiter").
    broken = '[{"action": "add" "content": "User prefers dark mode"}]'

    result = INLINE_TOOL_EXECUTORS["memory"](
        agent,
        {"target": "memory", "operations": broken},
        ctx,
    )

    payload = json.loads(result)
    assert payload["success"] is False, payload
    error = payload["error"]
    assert "operations" in error
    assert "list of objects" in error
    assert "action" in error and "content" in error and "old_text" in error
    assert store.memory_entries == []
    assert store.user_entries == []
