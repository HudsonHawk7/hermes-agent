import json
from agent.background_review import summarize_background_review_actions
from tools.skill_manager_tool import skill_manage


def _messages(args, data):
    return [{"role": "assistant", "tool_calls": [{"id": "skill", "function": {
        "name": "skill_manage", "arguments": json.dumps(args)}}]},
        {"role": "tool", "tool_call_id": "skill", "content": json.dumps(data)}]


def test_applied_skill_operations_notify_with_names(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    name = "notify-contract"
    content = f"---\nname: {name}\ndescription: Use when checking notices. Verify applied writes.\n---\nRead the sample before editing.\n"
    operations = [{"name": name, "action": "create", "content": content},
                  {"name": name, "action": "patch", "old_string": "sample", "new_string": "example"},
                  {"name": name, "action": "write_file", "file_path": "references/a.md", "file_content": "Check the example."},
                  {"name": name, "action": "remove_file", "file_path": "references/a.md"},
                  {"name": name, "action": "delete"}]
    for op in operations:
        data = json.loads(skill_manage(action="", name="", operations=[op]))
        assert data["success"], data
        messages = _messages({"operations": [op]}, data)
        for mode in ("on", "verbose"):
            actions = summarize_background_review_actions(messages, [], mode)
            assert actions and all(name in line and "?" not in line for line in actions)
        assert summarize_background_review_actions(messages, [], "off") == []
    assert not (tmp_path / "skills" / name).exists()


def test_unapplied_skill_operations_never_notify():
    args = {"operations": [{"name": "pending", "action": "create"}]}
    for data in (
        {"success": True, "staged": True, "message": "Write staged for approval."},
        {"success": False, "results": [{"success": True, "name": "pending", "action": "create"}]},
        {"success": True, "operations_applied": 0, "results": []},
        {"success": True, "operations_applied": 1, "results": [{"success": False, "name": "pending", "action": "create"}]},
    ):
        for mode in ("on", "verbose"):
            assert summarize_background_review_actions(_messages(args, data), [], mode) == []


def test_multi_file_skill_patch_collapses_duplicate_skill_verb_label(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    name = "force-session-ops"
    content = (
        f"---\nname: {name}\n"
        "description: Use when starting a Force session. Tracks work items.\n"
        "---\nCheck board rules first.\n"
    )
    create_res = json.loads(skill_manage(action="", name="", operations=[
        {"name": name, "action": "create", "content": content},
        {"name": name, "action": "write_file", "file_path": "references/force-work-item-tracking.md",
         "file_content": "Use ApprovedUtc when setting Approved.\n"},
    ]))
    assert create_res["success"], create_res

    patch_ops = [
        {"name": name, "action": "patch", "old_string": "Check board rules first.", "new_string": "Check board and schema rules first."},
        {"name": name, "action": "patch", "file_path": "references/force-work-item-tracking.md",
         "old_string": "Use ApprovedUtc when setting Approved.",
         "new_string": "Use ApprovedUtc (never UpdatedUtc) when setting Approved."},
    ]
    patch_res = json.loads(skill_manage(action="", name="", operations=patch_ops))
    assert patch_res["success"], patch_res

    actions = summarize_background_review_actions(_messages({"operations": patch_ops}, patch_res), [], "on")
    assert actions == [
        "Skill 'force-session-ops' patched (SKILL.md, references/force-work-item-tracking.md)"
    ]

    # Separate skill_manage calls in the same review pass also collapse rather than stuttering.
    separate_messages = [
        {"role": "assistant", "tool_calls": [{"id": "s1", "function": {
            "name": "skill_manage", "arguments": json.dumps({"operations": [patch_ops[0]]})}}]},
        {"role": "tool", "tool_call_id": "s1", "content": json.dumps({
            "success": True, "operations_applied": 1,
            "results": [{"success": True, "name": name, "action": "patch"}],
        })},
        {"role": "assistant", "tool_calls": [{"id": "s2", "function": {
            "name": "skill_manage", "arguments": json.dumps({"operations": [patch_ops[1]]})}}]},
        {"role": "tool", "tool_call_id": "s2", "content": json.dumps({
            "success": True, "operations_applied": 1,
            "results": [{"success": True, "name": name, "action": "patch",
                         "file_path": "references/force-work-item-tracking.md"}],
        })},
    ]
    assert summarize_background_review_actions(separate_messages, [], "on") == [
        "Skill 'force-session-ops' patched (SKILL.md, references/force-work-item-tracking.md)"
    ]
