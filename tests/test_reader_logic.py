from app.source.qq_window import normalize_text


def test_normalize_text_collapses_whitespace() -> None:
    assert normalize_text("  第一条\n  消息  ") == "第一条 消息"
