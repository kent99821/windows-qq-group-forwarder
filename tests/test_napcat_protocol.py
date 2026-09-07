from app.config import ListenerSession
from app.source.napcat_protocol import parse_message_event, render_segments, stable_message_key


GROUP = ListenerSession("group", "10001", "发家致富")
CONTACT = ListenerSession("private", "20002", "家欣")


def test_group_message_uses_card_before_nickname_and_stable_key() -> None:
    event = {
        "time": 1788698369,
        "self_id": 90001,
        "post_type": "message",
        "message_type": "group",
        "message_id": 123,
        "group_id": 10001,
        "group_name": "发家致富",
        "sender": {"user_id": 30003, "nickname": "QQ昵称", "card": "群名片"},
        "message": [{"type": "text", "data": {"text": "你好"}}],
    }

    message = parse_message_event(event, [GROUP])

    assert message is not None
    assert message.sender == "群名片"
    assert message.source_group == "发家致富"
    assert message.content == "你好"
    assert message.kind == "text"
    assert stable_message_key(event) == "napcat:90001:group:10001:123"


def test_private_message_uses_nickname_and_filters_other_conversations() -> None:
    event = {
        "time": 1788698369,
        "self_id": "90001",
        "post_type": "message",
        "message_type": "private",
        "message_id": 456,
        "user_id": "20002",
        "sender": {"user_id": "20002", "nickname": "家欣"},
        "message": [{"type": "text", "data": {"text": "在吗"}}],
    }

    message = parse_message_event(event, [CONTACT])

    assert message is not None
    assert message.sender == "家欣"
    assert message.source_group == "家欣"
    assert parse_message_event({**event, "user_id": "99999"}, [CONTACT]) is None


def test_segments_keep_order_and_render_all_members_mention() -> None:
    event = {
        "post_type": "message",
        "message_type": "group",
        "message_id": 1,
        "group_id": "10001",
        "sender": {"nickname": "小明"},
        "message": [
            {"type": "at", "data": {"qq": "all"}},
            {"type": "text", "data": {"text": " 请参加会议"}},
            {"type": "image", "data": {"file": "abc", "url": "https://example.invalid/a.jpg"}},
        ],
    }

    assert render_segments(event) == "@全体成员 请参加会议[图片]"
    message = parse_message_event(event, [GROUP])
    assert message is not None
    assert message.kind == "image"


def test_sent_and_non_message_events_are_ignored() -> None:
    event = {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "message_sent",
        "message_id": 2,
        "group_id": "10001",
        "message": "self",
    }
    assert parse_message_event(event, [GROUP]) is None
    assert parse_message_event({"post_type": "notice"}, [GROUP]) is None
