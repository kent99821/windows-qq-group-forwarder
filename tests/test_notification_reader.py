from app.config import SourceConfig
from app.source.windows_notification import WindowsNotificationReader


class FakeControl:
    def __init__(self, text: str) -> None:
        self._text = text

    def window_text(self) -> str:
        return self._text


class FakeRectangle:
    left = 0
    top = 0
    right = 460
    bottom = 180


class FakeElementInfo:
    class_name = "Windows.UI.Core.CoreWindow"


class FakeWindow:
    element_info = FakeElementInfo()

    def window_text(self) -> str:
        return "QQ"

    def rectangle(self) -> FakeRectangle:
        return FakeRectangle()

    def descendants(self, *, control_type: str) -> list[FakeControl]:
        assert control_type == "Text"
        return [FakeControl("QQ"), FakeControl("发家致富"), FakeControl("[特别关心] 家欣：哈哈")]


def source_config() -> SourceConfig:
    return SourceConfig("发家致富", "QQ", 1.0, ())


def test_notification_candidate_uses_group_and_body() -> None:
    reader = WindowsNotificationReader(source_config())
    result = reader._candidate(FakeWindow())
    assert result is not None
    assert result[3] == "[特别关心] 家欣：哈哈"
    assert result[2] == "发家致富"


def test_notification_message_has_toast_kind() -> None:
    reader = WindowsNotificationReader(source_config())
    message = reader._message("QQ", "新消息")
    assert message.kind == "toast_text"


class AggregatedWindow(FakeWindow):
    def descendants(self, *, control_type: str) -> list[FakeControl]:
        assert control_type == "Text"
        return [
            FakeControl("QQ"),
            FakeControl("发家致富"),
            FakeControl("家欣：目标群消息"),
            FakeControl("二群"),
            FakeControl("小明：其他群消息"),
        ]


class OtherGroupWindow(FakeWindow):
    def descendants(self, *, control_type: str) -> list[FakeControl]:
        assert control_type == "Text"
        return [FakeControl("QQ"), FakeControl("二群"), FakeControl("小明：不能转发")]


def test_aggregated_notifications_only_extract_configured_group() -> None:
    reader = WindowsNotificationReader(source_config())
    result = reader._candidate(AggregatedWindow())
    assert result is not None
    assert result[3] == "家欣：目标群消息"


def test_aggregated_notifications_keep_two_same_image_messages() -> None:
    class TwoImagesWindow(FakeWindow):
        def descendants(self, *, control_type: str) -> list[FakeControl]:
            assert control_type == "Text"
            return [
                FakeControl("QQ"),
                FakeControl("发家致富"),
                FakeControl("家欣：[图片]"),
                FakeControl("家欣：[图片]"),
            ]

    reader = WindowsNotificationReader(source_config())
    reader._primed = True
    reader._scan = lambda: reader._candidate_items(TwoImagesWindow())  # type: ignore[method-assign]
    assert len(reader.poll()) == 2


def test_other_group_notification_is_rejected() -> None:
    reader = WindowsNotificationReader(source_config())
    assert reader._candidate(OtherGroupWindow()) is None


def test_user_notification_listener_parser_uses_app_and_notification_texts() -> None:
    items = WindowsNotificationReader._candidate_items_from_texts(
        source_config(),
        "winrt:QQ:123",
        "QQ",
        ["发家致富", "家欣：WinRT API 消息"],
        "UserNotification",
    )

    assert items == [("winrt:QQ:123:item:0", "QQ", "发家致富", "家欣：WinRT API 消息", "UserNotification")]


def test_multiple_configured_groups_keep_their_source_group() -> None:
    config = SourceConfig("发家致富", "QQ", 1.0, (), group_names=("发家致富", "二群"))
    items = WindowsNotificationReader._candidate_items_from_texts(
        config,
        "winrt:QQ:123",
        "QQ",
        ["发家致富", "家欣：第一条", "二群", "小明：第二条"],
        "UserNotification",
    )

    assert items == [
        ("winrt:QQ:123:item:0", "QQ", "发家致富", "家欣：第一条", "UserNotification"),
        ("winrt:QQ:123:item:1", "QQ", "二群", "小明：第二条", "UserNotification"),
    ]


def test_contact_notification_without_sender_prefix_is_listened() -> None:
    config = SourceConfig("发家致富", "QQ", 1.0, (), listener_names=("发家致富", "家欣"))

    items = WindowsNotificationReader._candidate_items_from_texts(
        config,
        "winrt:QQ:456",
        "QQ",
        ["家欣", "你好，这是联系人消息"],
        "UserNotification",
    )

    assert items == [
        ("winrt:QQ:456:item:0", "QQ", "家欣", "你好，这是联系人消息", "UserNotification"),
    ]


def test_contact_notification_uses_session_name_as_sender() -> None:
    reader = WindowsNotificationReader(source_config())

    message = reader._message(
        "winrt:QQ:456:item:0",
        "QQ",
        "[特别关心] 使用UI方式",
        source_group="家欣",
    )

    assert message.sender == "家欣"


def test_empty_notification_api_falls_back_to_visible_toast() -> None:
    reader = WindowsNotificationReader(source_config())
    api_backend = type("FakeApiBackend", (), {"scan": lambda self: []})()
    visible_toast = [("runtime:toast", "QQ", "发家致富", "家欣：可见通知", "Windows.UI.Core.CoreWindow")]
    reader._api_backend = api_backend
    reader._scan_uia = lambda: visible_toast  # type: ignore[method-assign]

    assert reader._scan() == visible_toast
    assert reader.backend_name == "windows-user-notification-listener+uia-fallback"


def test_image_placeholder_has_distinct_kind() -> None:
    reader = WindowsNotificationReader(source_config())
    message = reader._message("QQ", "家欣：[图片]")
    assert message.kind == "toast_image_notice"


def test_same_toast_content_is_not_repeated_when_identity_changes() -> None:
    reader = WindowsNotificationReader(source_config())
    scans = iter([
        [("runtime:1", "QQ", "发家致富", "家欣：同一条", "toast")],
        [("runtime:2", "QQ", "发家致富", "家欣：同一条", "toast")],
    ])
    reader._primed = True
    reader._scan = lambda: next(scans)  # type: ignore[method-assign]
    assert len(reader.poll()) == 1
    assert reader.poll() == []
