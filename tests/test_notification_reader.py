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
    assert result[2] == "[特别关心] 家欣：哈哈"


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
    assert result[2] == "家欣：目标群消息"


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


def test_image_placeholder_has_distinct_kind() -> None:
    reader = WindowsNotificationReader(source_config())
    message = reader._message("QQ", "家欣：[图片]")
    assert message.kind == "toast_image_notice"


def test_same_toast_content_is_not_repeated_when_identity_changes() -> None:
    reader = WindowsNotificationReader(source_config())
    scans = iter([
        [("runtime:1", "QQ", "家欣：同一条", "toast")],
        [("runtime:2", "QQ", "家欣：同一条", "toast")],
    ])
    reader._primed = True
    reader._scan = lambda: next(scans)  # type: ignore[method-assign]
    assert len(reader.poll()) == 1
    assert reader.poll() == []
