from app.models import IncomingMessage
from app.source.qq_history_reader import (
    HistoryRecord,
    UiNode,
    bootstrap_history_from_notifications,
    history_delta,
    merge_history_snapshots,
    merge_scrolled_history_snapshots,
    merge_notifications_with_history,
    parse_history_nodes,
    _record_key,
    QqHistoryReader,
)
from app.state_store import StateStore


def node(
    control_type: str,
    text: str = "",
    rect: tuple[int, int, int, int] = (0, 0, 0, 0),
    children: tuple[UiNode, ...] = (),
) -> UiNode:
    return UiNode(control_type, text, *rect, children)


def test_parse_history_keeps_incoming_messages_in_order() -> None:
    nodes = [
        node("Group", rect=(0, 10, 1000, 35), children=(node("Text", "13:49"),)),
        node("Group", "家欣", (20, 50, 60, 90)),
        node("Text", "家欣", (80, 48, 120, 70)),
        node("Group", rect=(80, 74, 180, 120), children=(node("Text", "你好"),)),
        node("Group", "家欣", (20, 130, 60, 170)),
        node("Text", "家欣", (80, 128, 120, 150)),
        node("Group", rect=(80, 154, 180, 200), children=(node("Text", "在吗"),)),
        node("Text", "我", (850, 210, 880, 230)),
        node("Group", rect=(700, 234, 900, 280), children=(node("Text", "自己的消息"),)),
        node("Group", rect=(0, 0, 0, 0), children=(node("Text", "不可见历史"),)),
    ]

    records = parse_history_nodes(nodes, 0, 1000, "发家致富")

    assert [(record.sender, record.content, record.display_time) for record in records] == [
        ("家欣", "你好", "13:49"),
        ("家欣", "在吗", "13:49"),
    ]


def test_parse_history_recognizes_incoming_image() -> None:
    records = parse_history_nodes(
        [
            node("Text", "家欣", (80, 48, 120, 70)),
            node("Group", rect=(80, 74, 220, 180), children=(node("Image", "图片"),)),
        ],
        0,
        1000,
        "发家致富",
    )

    assert [(record.content, record.kind) for record in records] == [
        ("[图片]", "toast_image_notice"),
    ]


def test_parse_history_uses_avatar_accessible_name_as_sender() -> None:
    records = parse_history_nodes(
        [
            node("Group", "家欣", (20, 50, 60, 90)),
            node("Group", rect=(80, 74, 180, 120), children=(node("Text", "你好"),)),
        ],
        0,
        1000,
        "发家致富",
    )

    assert [(record.sender, record.content) for record in records] == [("家欣", "你好")]


def test_parse_history_ignores_member_role_badge_after_nickname() -> None:
    records = parse_history_nodes(
        [
            node("Text", "元来！", (80, 48, 125, 70)),
            node("Text", "管理员", (130, 48, 180, 70)),
            node("Group", rect=(80, 74, 300, 130), children=(node("Text", "这几天跟上节奏"),)),
        ],
        0,
        1000,
        "元子大讲堂9月主升",
    )

    assert [(record.sender, record.content) for record in records] == [("元来！", "这几天跟上节奏")]


def test_parse_history_distinguishes_repeated_visible_messages() -> None:
    records = parse_history_nodes(
        [
            node("Group", "家欣", (20, 50, 60, 90)),
            node("Group", rect=(80, 74, 180, 120), children=(node("Text", "你好"),)),
            node("Group", "家欣", (20, 130, 60, 170)),
            node("Group", rect=(80, 154, 180, 200), children=(node("Text", "你好"),)),
        ],
        0,
        1000,
        "发家致富",
    )

    assert [record.occurrence for record in records] == [1, 2]


def test_history_backfill_replaces_covered_notification() -> None:
    notification = IncomingMessage.create("toast-last", "发家致富", "家欣：在吗")
    history = [
        HistoryRecord("发家致富", "家欣", "你好", "13:49", "history_text", 1),
        HistoryRecord("发家致富", "家欣", "在吗", "13:49", "history_text", 1),
    ]

    merged = merge_notifications_with_history([notification], history, history)

    assert [(message.sender, message.content) for message in merged] == [
        ("家欣", "你好"),
        ("家欣", "在吗"),
    ]


def test_stable_snapshots_keep_rows_that_scroll_out_while_new_rows_arrive() -> None:
    first = [
        HistoryRecord("发家致富", "家欣", f"第{i}条", "19:13", "history_text", 1)
        for i in range(1, 4)
    ]
    second = [
        HistoryRecord("发家致富", "家欣", f"第{i}条", "19:13", "history_text", 1)
        for i in range(2, 6)
    ]

    assert [record.content for record in merge_history_snapshots([first, second])] == [
        "第1条",
        "第2条",
        "第3条",
        "第4条",
        "第5条",
    ]


def test_scrolled_snapshots_are_reassembled_from_oldest_to_newest() -> None:
    newest = [
        HistoryRecord("发家致富", "家欣", f"第{i}条", "19:13", "history_text", 1)
        for i in (7, 8, 9)
    ]
    older = [
        HistoryRecord("发家致富", "家欣", f"第{i}条", "19:13", "history_text", 1)
        for i in (4, 5, 6, 7)
    ]
    oldest = [
        HistoryRecord("发家致富", "家欣", f"第{i}条", "19:13", "history_text", 1)
        for i in (1, 2, 3, 4)
    ]

    merged = merge_scrolled_history_snapshots([newest, older, oldest])

    assert [record.content for record in merged] == [f"第{i}条" for i in range(1, 10)]


def test_scrolled_snapshots_do_not_duplicate_a_repeated_page() -> None:
    page = [
        HistoryRecord("发家致富", "家欣", f"第{i}条", "19:13", "history_text", 1)
        for i in (1, 2, 3)
    ]

    merged = merge_scrolled_history_snapshots([page, page])

    assert [record.content for record in merged] == ["第1条", "第2条", "第3条"]


def test_persistent_source_watermark_returns_only_following_history(tmp_path) -> None:
    store = StateStore(tmp_path / "state.sqlite3")
    try:
        reader = QqHistoryReader.__new__(QqHistoryReader)
        reader.store = store
        reader._primed = {"发家致富"}
        reader._last_visible = {}
        old = HistoryRecord("发家致富", "家欣", "旧消息", "12:00", "history_text", 1)
        new = HistoryRecord("发家致富", "家欣", "新消息", "12:01", "history_text", 1)
        store.set_history_watermark("发家致富", _record_key(old))
        reader.read_visible = lambda source_group, settle_seconds=0.0: [old, new]

        records, visible = reader._new_history("发家致富", [])

        assert [record.content for record in records] == ["新消息"]
        assert visible == [old, new]
    finally:
        store.close()


def test_failed_startup_baseline_recovers_whole_notification_time_bucket() -> None:
    visible = [
        HistoryRecord("发家致富", "家欣", "旧消息", "18:20", "history_text", 1),
        *[
            HistoryRecord("发家致富", "家欣", f"多条消息，第{i}条", "19:13", "history_text", 1)
            for i in range(1, 6)
        ],
    ]
    notification = IncomingMessage.create(
        "toast-third",
        "发家致富",
        "[特别关心] 家欣：多条消息，第3条",
    )

    recovered = bootstrap_history_from_notifications([notification], visible)

    assert [record.content for record in recovered] == [
        "多条消息，第1条",
        "多条消息，第2条",
        "多条消息，第3条",
        "多条消息，第4条",
        "多条消息，第5条",
    ]


def test_history_delta_uses_sequence_overlap_when_top_metadata_scrolls_out() -> None:
    previous = [
        HistoryRecord("发家致富", "家欣", "多条消息，第4条", "19:13", "history_text", 1),
        HistoryRecord("发家致富", "家欣", "多条消息，第5条", "19:13", "history_text", 1),
    ]
    current = [
        HistoryRecord("发家致富", "发家致富", "多条消息，第4条", "", "history_text", 1),
        HistoryRecord("发家致富", "家欣", "多条消息，第5条", "", "history_text", 1),
        *[
            HistoryRecord("发家致富", "家欣", value, "20:38", "history_text", 1)
            for value in ("111", "222", "333", "444", "555", "666")
        ],
    ]

    assert [record.content for record in history_delta(previous, current)] == [
        "111",
        "222",
        "333",
        "444",
        "555",
        "666",
    ]


def test_seen_history_still_suppresses_delayed_duplicate_notification() -> None:
    notification = IncomingMessage.create("toast-last", "发家致富", "家欣：在吗")
    visible = [HistoryRecord("发家致富", "家欣", "在吗", "13:49", "history_text", 1)]

    assert merge_notifications_with_history([notification], [], visible) == []


def test_special_care_prefix_does_not_duplicate_backfilled_message() -> None:
    notification = IncomingMessage.create(
        "toast-last",
        "发家致富",
        "[特别关心] 家欣：在吗",
    )
    visible = [HistoryRecord("发家致富", "家欣", "在吗", "13:49", "history_text", 1)]

    merged = merge_notifications_with_history([notification], visible, visible)

    assert [(message.sender, message.content) for message in merged] == [("家欣", "在吗")]


def test_image_placeholder_is_not_mistaken_for_notification_badge() -> None:
    notification = IncomingMessage.create(
        "toast-image",
        "发家致富",
        "元子小助理：[图片]",
        kind="toast_image_notice",
    )
    visible = [
        HistoryRecord(
            "发家致富",
            "元子小助理",
            "[图片]",
            "23:40",
            "toast_image_notice",
            1,
        )
    ]

    merged = merge_notifications_with_history([notification], visible, visible)

    assert [(message.sender, message.content) for message in merged] == [
        ("元子小助理", "[图片]")
    ]


def test_notification_is_used_when_history_read_fails() -> None:
    notification = IncomingMessage.create("toast-only", "发家致富", "家欣：在吗")

    assert merge_notifications_with_history([notification], [], None) == [notification]
