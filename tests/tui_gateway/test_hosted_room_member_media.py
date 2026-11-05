"""A group-chat member's ``MEDIA:`` reply lines become room attachments, or one-line refusals."""

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway import hosted_room_driver as driver, hosted_rooms
from gateway.hosted_room_attachments import AttachmentError, AttachmentQuotaError, HostedRoomAttachmentStore
from gateway.hosted_room_discussion import DiscussionMember, EventPlan
from tui_gateway.hosted_room_service import HostedRoomService, _room_media_tags

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\0" * 32
PEER_DIGEST = "a" * 64


def _png(size):
    return PNG + b"\0" * (size - len(PNG))


class World:
    """One service whose ``default`` member may share from ``output`` and whose ``solara`` and ``aura``
    profiles each have their own folder."""

    def __init__(self, tmp_path):
        self.tmp_path, self.db = tmp_path, tmp_path / "state.db"
        self.output = tmp_path / "output"
        self.output.mkdir()
        (tmp_path / "config.yaml").write_text(
            f"image_gen:\n  test:\n    output_dir: {self.output}\n", encoding="utf-8")
        self.profile_output = {}
        for profile in ("solara", "aura"):
            home = tmp_path / "profiles" / profile
            folder = home / "renders"
            folder.mkdir(parents=True)
            (home / "config.yaml").write_text(
                f"image_gen:\n  test:\n    output_dir: {folder}\n", encoding="utf-8")
            self.profile_output[profile] = folder
        self.service = HostedRoomService(SimpleNamespace(), db_path=self.db)
        self.service.local_profiles = lambda: ("default", "solara", "aura")
        self.store = HostedRoomAttachmentStore(self.db)
        self.room = hosted_rooms.create_room(
            self.db, room_id="room", name="Room", authority_gateway_id="gateway",
            members=[{"member_id": "member", "profile": "default", "handle": "member"},
                     {"member_id": "other", "profile": "aura", "handle": "other"}])

    def picture(self, name="picture.png", data=PNG, folder=None):
        path = (folder or self.output) / name
        path.write_bytes(data)
        return path

    def share(self, text, *, profile="default", target=None, event_id="member-message"):
        member = DiscussionMember("member", profile, "member", target=target)
        return self.service._member_media_event(self.event(text, event_id), self.room, member)

    @staticmethod
    def event(text, event_id="member-message"):
        return EventPlan(
            event_id=event_id, kind="message.member",
            actor={"kind": "member", "id": "member", "profile": "default"},
            payload={"text": text, "discussion_event_id": "discussion", "member_id": "member", "member_index": 0,
                     "round_index": 0, "task_id": "task", "thread_id": "thread", "turn_id": "turn"},
            authority_gateway_id="gateway", authority_epoch=1)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _no_media_line(text):
    return not any(line.lstrip(" \t`\"'*_").startswith("MEDIA:") for line in text.splitlines())


def test_member_media_in_its_output_folder_becomes_a_room_attachment(world):
    picture = world.picture()

    event, committed = world.share(f"Here it is\nMEDIA:{picture}")

    assert event.payload["text"] == "Here it is"
    assert event.payload["attachments"] == [{
        "attachment_id": event.payload["attachments"][0]["attachment_id"], "kind": "image",
        "name": "picture.png", "size": len(PNG), "mime": "image/png"}]
    assert committed == (event.payload["attachments"][0]["attachment_id"],)
    saved = hosted_rooms.append_event(world.db, **event.append_kwargs("room"))
    assert saved["payload"]["attachments"] == event.payload["attachments"]


def test_a_text_only_member_reply_passes_through_unchanged(world):
    event = world.event("Just words, and a path /tmp/x.png with no tag")

    result, committed = world.service._member_media_event(
        event, world.room, DiscussionMember("member", "default", "member"))

    assert result is event
    assert committed == ()


def test_a_video_is_shared_inline_as_a_video_attachment(world):
    clip = world.picture("clip.mp4", MP4)

    event, _ = world.share(f"MEDIA:{clip}")

    assert event.payload["text"] == ""
    assert [(a["kind"], a["mime"], a["name"]) for a in event.payload["attachments"]] == [
        ("video", "video/mp4", "clip.mp4")]


def test_a_refused_video_says_video_and_a_refused_picture_says_picture(world, tmp_path):
    clip, still = tmp_path / "clip.mp4", tmp_path / "still.png"
    clip.write_bytes(MP4)
    still.write_bytes(PNG)

    event, _ = world.share(f"MEDIA:{clip}\nMEDIA:{still}")

    assert event.payload["text"].splitlines() == [
        "(video not shared: outside allowed folders)", "(picture not shared: outside allowed folders)"]
    assert "attachments" not in event.payload


@pytest.mark.parametrize("name", ["song.mp3", "report.pdf", "notes.txt"])
def test_a_non_visual_file_says_file_never_picture(world, name):
    path = world.picture(name, b"%PDF-1.7\n")

    event, _ = world.share(f"MEDIA:{path}")

    assert event.payload["text"] == "(file not shared: unsupported type)"


def test_a_file_outside_every_allowed_folder_is_refused(world, tmp_path):
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)

    event, committed = world.share(f"MEDIA:{outside}")

    assert event.payload["text"] == "(picture not shared: outside allowed folders)"
    assert "attachments" not in event.payload
    assert committed == ()


def test_screen_share_frames_are_never_room_media(world, tmp_path):
    shots = tmp_path / "shots"
    shots.mkdir()
    (tmp_path / "config.yaml").write_text(
        f"image_gen:\n  test:\n    output_dir: {world.output}\n    screenshots_dir: {shots}\n"
        f"video_gen:\n  test:\n    screenshots_dir: {shots}\n", encoding="utf-8")
    frame = world.picture("frame-0001.png", folder=shots)

    event, _ = world.share(f"MEDIA:{frame}")

    assert event.payload["text"] == "(picture not shared: outside allowed folders)"
    assert world.store.stats(room_id="room")["attachments"] == 0


def test_one_profile_cannot_share_another_profiles_renders(world):
    aura_render = world.picture("aura.png", folder=world.profile_output["aura"])
    own_render = world.picture("solara.png", folder=world.profile_output["solara"])

    refused, _ = world.share(f"MEDIA:{aura_render}", profile="solara")
    shared, _ = world.share(f"MEDIA:{own_render}", profile="solara", event_id="own")

    assert refused.payload["text"] == "(picture not shared: outside allowed folders)"
    assert [a["name"] for a in shared.payload["attachments"]] == ["solara.png"]


def test_a_symlink_inside_an_allowed_folder_that_leads_outside_is_refused(world, tmp_path):
    real = tmp_path / "secret.png"
    real.write_bytes(PNG)
    link = world.output / "looks-inside.png"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create a symlink here: {exc}")

    event, _ = world.share(f"MEDIA:{link}")

    assert event.payload["text"] == "(picture not shared: outside allowed folders)"
    assert "attachments" not in event.payload


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows reparse point")
def test_a_junction_inside_an_allowed_folder_that_leads_outside_is_refused(world, tmp_path):
    import _winapi

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(PNG)
    _winapi.CreateJunction(str(outside), str(world.output / "jump"))

    event, _ = world.share(f"MEDIA:{world.output / 'jump' / 'secret.png'}")

    assert event.payload["text"] == "(picture not shared: outside allowed folders)"
    assert "attachments" not in event.payload


def test_a_peer_members_paths_are_never_resolved_on_this_machine(world, monkeypatch):
    picture = world.picture()
    from tools import preview_upload

    def touched(*args, **kwargs):
        raise AssertionError("a peer member's path was read on this host")

    monkeypatch.setattr(preview_upload, "read_upload", touched)
    target = {"kind": "peer", "peer_id": "peer-1", "installation_id": "install-1",
              "profile": "default", "capability_digest": PEER_DIGEST}

    event, committed = world.share(f"Look\nMEDIA:{picture}\nMEDIA:{world.output / 'b.mp4'}", target=target)

    assert event.payload["text"].splitlines() == [
        "Look", "(picture not shared: sent from another machine)", "(video not shared: sent from another machine)"]
    assert "attachments" not in event.payload
    assert committed == ()


def test_a_local_member_with_no_home_gets_a_refusal_never_this_hosts_root(world):
    picture = world.picture()

    event, _ = world.share(f"MEDIA:{picture}", profile="ghost")

    assert event.payload["text"] == "(picture not shared: member profile is unavailable)"
    assert "attachments" not in event.payload


def test_a_file_over_the_per_file_cap_is_refused_as_too_large(world):
    big = world.picture("big.png", _png(15_000_001))

    event, _ = world.share(f"MEDIA:{big}")

    assert event.payload["text"] == "(picture not shared: too large)"
    assert world.store.stats(room_id="room")["attachments"] == 0


def test_files_that_fit_are_kept_when_the_message_total_would_overflow(world):
    first = world.picture("first.png", _png(13_000_000))
    second = world.picture("second.png", _png(13_000_000) + b"\1")
    third = world.picture("third.png", _png(1_000))

    event, _ = world.share(f"MEDIA:{first}\nMEDIA:{second}\nMEDIA:{third}")

    assert [a["name"] for a in event.payload["attachments"]] == ["first.png", "third.png"]
    assert event.payload["text"] == "(picture not shared: too large for one message)"


def test_more_than_eight_tags_keep_eight_and_refuse_the_rest(world):
    tags = [f"MEDIA:{world.picture(f'p{n}.png', PNG + bytes([n]))}" for n in range(10)]

    event, _ = world.share("\n".join(tags))

    assert len(event.payload["attachments"]) == 8
    assert event.payload["text"].splitlines() == ["(picture not shared: attachment limit reached)"] * 2


def test_a_full_room_store_is_reported_as_storage_not_size(world, monkeypatch):
    first, second = world.picture("a.png", PNG + b"1"), world.picture("b.png", PNG + b"2")
    put = HostedRoomAttachmentStore.put

    def quota_after_one(self, **kwargs):
        if kwargs["upload_id"].endswith(":1"):
            raise AttachmentQuotaError("room attachment quota exceeded")
        return put(self, **kwargs)

    monkeypatch.setattr(HostedRoomAttachmentStore, "put", quota_after_one)

    event, _ = world.share(f"MEDIA:{first}\nMEDIA:{second}")

    assert [a["name"] for a in event.payload["attachments"]] == ["a.png"]
    assert event.payload["text"] == "(picture not shared: room storage is full)"


def test_a_failed_commit_names_the_video_and_frees_the_uploads(world, monkeypatch):
    clip, still = world.picture("clip.mp4", MP4), world.picture("still.png")

    def failing(self, **kwargs):
        raise AttachmentError("attachment has expired")

    monkeypatch.setattr(HostedRoomAttachmentStore, "commit_message_with_receipt", failing)

    event, committed = world.share(f"MEDIA:{clip}\nMEDIA:{still}")

    assert event.payload["text"].splitlines() == [
        "(video not shared: could not be verified)", "(picture not shared: could not be verified)"]
    assert "attachments" not in event.payload
    assert committed == ()
    assert world.store.stats(room_id="room") == {
        "attachments": 0, "logical_bytes": 0, "blobs": 0, "physical_bytes": 0}


@pytest.mark.parametrize("tag", [
    "MEDIA:\\\\uncprobe-host\\share\\pic.png",
    "MEDIA://uncprobe-host/share/pic.png",
    "MEDIA:/uncprobe-host/share/Caddyfile",
    "MEDIA:/uncprobe-host/share/notes.log",
    "MEDIA:\\\\uncprobe-host\\share\\Caddyfile",
    "MEDIA:/uncprobe-host/does/not/exist",
])
def test_a_network_or_extensionless_tag_is_refused_without_touching_the_filesystem(world, monkeypatch, tag):
    from gateway.platforms import base

    probed = []

    def spy(owner, name):
        original = getattr(owner, name)

        def wrapper(*args, **kwargs):
            if "uncprobe-host" in " ".join(str(arg) for arg in args):
                probed.append(name)
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, wrapper)

    for owner, name in [
        (os, "stat"), (os, "lstat"), (os.path, "exists"), (os.path, "isfile"), (os.path, "isdir"),
        (os.path, "realpath"), (Path, "resolve"), (Path, "stat"), (Path, "exists"), (Path, "is_file"),
        (base, "validate_media_delivery_path"), (base, "_resolve_path")]:
        spy(owner, name)

    event, _ = world.share(f"Before\n{tag}\nAfter")

    assert probed == []
    assert event.payload["text"].splitlines()[0] == "Before"
    assert event.payload["text"].splitlines()[-1].endswith(")")
    assert _no_media_line(event.payload["text"])
    assert "attachments" not in event.payload


@pytest.mark.parametrize("text, refusals", [
    ("MEDIA:/nowhere/at/all/Caddyfile", 1),
    ("MEDIA:/nowhere/missing.png", 1),
    ("MEDIA:relative/pic.png", 1),
    ("MEDIA:", 1),
    ("  **MEDIA:/nowhere/at/all/Makefile**", 1),
])
def test_no_media_line_survives_processing(world, text, refusals):
    event, _ = world.share(f"intro\n{text}\noutro")

    lines = event.payload["text"].splitlines()
    assert _no_media_line(event.payload["text"])
    assert sum(line.endswith(")") and "not shared" in line for line in lines) == refusals
    assert lines[0] == "intro"


def test_a_tag_inside_a_code_block_is_an_example_not_a_share(world):
    text = "Use it like:\n```\nMEDIA:/nowhere/example.png\n```"

    paths, cleaned = _room_media_tags(text)

    assert paths == []
    assert cleaned == text


def test_replaying_an_event_reuses_the_stored_payload_when_the_file_is_gone(world):
    picture = world.picture()
    first, _ = world.share(f"Here\nMEDIA:{picture}")
    hosted_rooms.append_event(world.db, **first.append_kwargs("room"))
    picture.write_bytes(PNG + b"changed")
    picture.unlink()

    replay, committed = world.share(f"Here\nMEDIA:{picture}")

    assert replay.payload == first.payload
    assert committed == ()
    assert hosted_rooms.append_event(world.db, **replay.append_kwargs("room"))["idempotent"] is True


def _settle(world, text):
    """A real queued member task settled with ``text``, ready for the service to publish."""
    service = world.service
    service.create_room(room_id="wired", name="Wired", members=[
        {"member_id": "default", "profile": "default", "handle": "hermes"},
        {"member_id": "aura", "profile": "aura", "handle": "aura"}])
    service.send(room_id="wired", event_id="ask", payload={"text": "@hermes show me", "thread_id": "thread"})
    binding = service.bindings()[0]
    task = driver.list_tasks(world.db, room_id="wired", status="queued")[0]
    lease = driver.acquire_lease(
        world.db, room_id="wired", gateway_id=binding.gateway_id, authority_epoch=binding.authority_epoch,
        process_generation="test", ttl_seconds=300, clock=time.time)
    attempt = driver.start_task(world.db, task["identity"], lease, expected_cancel_generation=0, clock=time.time)
    driver.settle_task(world.db, attempt, settlement_id="result", status="settled",
                       result={"text": text}, clock=time.time)
    return binding


def _member_messages(world):
    return [e for e in world.service._events("wired") if e["kind"] == "message.member"]


def test_a_settled_member_reply_is_published_with_its_media_attached(world):
    picture = world.picture()
    binding = _settle(world, f"Done\nMEDIA:{picture}")

    world.service.prepare_room(binding)

    message, = _member_messages(world)
    assert message["payload"]["text"] == "Done"
    attachment, = message["payload"]["attachments"]
    assert (attachment["name"], attachment["kind"]) == ("picture.png", "image")
    viewed = world.store.read_viewer(
        room_id="wired", attachment_id=attachment["attachment_id"], event_id=message["event_id"],
        authority_gateway_id=binding.gateway_id, authority_epoch=binding.authority_epoch)
    assert viewed.data == PNG


def test_a_failed_append_releases_the_committed_attachments_and_the_next_cycle_publishes(world, monkeypatch):
    picture = world.picture()
    binding = _settle(world, f"Done\nMEDIA:{picture}")
    append = hosted_rooms.append_event
    failures = []

    def flaky(*args, **kwargs):
        if kwargs.get("kind") == "message.member" and not failures:
            failures.append(True)
            raise OSError("disk went away")
        return append(*args, **kwargs)

    monkeypatch.setattr(hosted_rooms, "append_event", flaky)
    with pytest.raises(OSError, match="disk went away"):
        world.service.prepare_room(binding)

    assert world.store.stats(room_id="wired") == {
        "attachments": 0, "logical_bytes": 0, "blobs": 0, "physical_bytes": 0}
    assert _member_messages(world) == []

    world.service.prepare_room(binding)

    message, = _member_messages(world)
    assert len(message["payload"]["attachments"]) == 1


def test_a_replay_after_a_lost_terminal_append_does_not_wedge_the_room(world, monkeypatch):
    picture = world.picture()
    binding = _settle(world, f"Done\nMEDIA:{picture}")
    append = hosted_rooms.append_event

    def lose_terminal(*args, **kwargs):
        if kwargs.get("kind") == "turn.settled":
            raise OSError("lost terminal append")
        return append(*args, **kwargs)

    with monkeypatch.context() as crash:
        crash.setattr(hosted_rooms, "append_event", lose_terminal)
        with pytest.raises(OSError, match="lost terminal"):
            world.service.prepare_room(binding)
    stored, = _member_messages(world)
    assert len(stored["payload"]["attachments"]) == 1
    picture.unlink()

    world.service.prepare_room(binding)

    message, = _member_messages(world)
    assert message["payload"] == stored["payload"]
    assert [e["kind"] for e in world.service._events("wired")].count("turn.settled") == 1
