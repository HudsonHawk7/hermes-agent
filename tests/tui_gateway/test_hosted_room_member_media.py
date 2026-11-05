from types import SimpleNamespace

from gateway import hosted_rooms
from gateway.hosted_room_discussion import DiscussionMember, EventPlan
from tui_gateway.hosted_room_service import HostedRoomService


PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


def _room(db):
    return hosted_rooms.create_room(
        db, room_id="room", name="Room", authority_gateway_id="gateway",
        members=[{"member_id": "member", "profile": "default", "handle": "member"},
                 {"member_id": "peer", "profile": "peer", "handle": "peer"}],
    )


def _event(text):
    return EventPlan(
        event_id="member-message", kind="message.member",
        actor={"kind": "member", "id": "member", "profile": "default"},
        payload={"text": text, "discussion_event_id": "discussion", "member_id": "member", "member_index": 0,
                 "round_index": 0, "task_id": "task", "thread_id": "thread", "turn_id": "turn"},
        authority_gateway_id="gateway", authority_epoch=1)


def test_member_media_in_its_output_folder_becomes_a_room_attachment(tmp_path):
    db, output = tmp_path / "state.db", tmp_path / "output"
    output.mkdir()
    picture = output / "picture.png"
    picture.write_bytes(PNG)
    (tmp_path / "config.yaml").write_text(f"image_gen:\n  test:\n    output_dir: {output}\n", encoding="utf-8")
    room = _room(db)
    service = HostedRoomService(SimpleNamespace(), db_path=db)

    event, _ = service._member_media_event(
        _event(f"Here it is\nMEDIA:{picture}"), room, DiscussionMember("member", "default", "member"))

    assert event.payload["text"] == "Here it is"
    assert event.payload["attachments"] == [{
        "attachment_id": event.payload["attachments"][0]["attachment_id"], "kind": "image",
        "name": "picture.png", "size": len(PNG), "mime": "image/png"}]
    saved = hosted_rooms.append_event(db, **event.append_kwargs("room"))
    assert saved["payload"]["attachments"] == event.payload["attachments"]


def test_member_media_outside_its_allowlist_is_replaced_with_a_notice(tmp_path):
    db, output = tmp_path / "state.db", tmp_path / "output"
    output.mkdir()
    picture = tmp_path / "outside.png"
    picture.write_bytes(PNG)
    (tmp_path / "config.yaml").write_text(f"image_gen:\n  test:\n    output_dir: {output}\n", encoding="utf-8")
    room = _room(db)
    service = HostedRoomService(SimpleNamespace(), db_path=db)

    event, _ = service._member_media_event(
        _event(f"MEDIA:{picture}"), room, DiscussionMember("member", "default", "member"))

    assert event.payload.get("attachments", []) == []
    assert event.payload["text"] == "(picture not shared: outside allowed folders)"
