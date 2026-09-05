from pathlib import Path

from app.config import SourceConfig
from app.source.qq_image_cache import QqImageCache
from app.source.qq_window_image import QqWindowImageReader


def source_config(cache_path: Path) -> SourceConfig:
    return SourceConfig(
        "发家致富",
        "QQ",
        1.0,
        (),
        image_cache_paths=(cache_path,),
        image_cache_match_seconds=20.0,
        image_cache_settle_seconds=0.0,
        image_cache_wait_seconds=0.0,
    )


def test_finds_new_jpeg_after_baseline(tmp_path: Path) -> None:
    cache_path = tmp_path / "nt_data" / "Pic"
    cache_path.mkdir(parents=True)
    cache = QqImageCache(source_config(cache_path))
    cache.prime()

    image = cache_path / "new-image.jpg"
    image.write_bytes(b"\xff\xd8\xff" + b"x" * 256)
    found = cache.find_for_notification()

    assert found == image


def test_ignores_existing_image_from_baseline(tmp_path: Path) -> None:
    cache_path = tmp_path / "Pic"
    cache_path.mkdir()
    image = cache_path / "old.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 256)
    cache = QqImageCache(source_config(cache_path))
    cache.prime()
    assert cache.find_for_notification() is None


def test_stages_image_for_retry(tmp_path: Path) -> None:
    cache_path = tmp_path / "Pic"
    cache_path.mkdir()
    source = cache_path / "image.webp"
    source.write_bytes(b"RIFF" + b"x" * 4 + b"WEBP" + b"x" * 256)
    cache = QqImageCache(source_config(cache_path))
    target = cache.stage(source, "message-key", tmp_path / "image-cache")
    assert target.exists()
    assert target.read_bytes() == source.read_bytes()


def test_prefers_ori_over_thumb(tmp_path: Path) -> None:
    cache_path = tmp_path / "Pic"
    ori = cache_path / "Ori"
    thumb = cache_path / "Thumb"
    ori.mkdir(parents=True)
    thumb.mkdir()
    cache = QqImageCache(source_config(cache_path))
    cache.prime()
    (thumb / "same.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"t" * 200)
    (ori / "same.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"o" * 200)
    assert cache.find_for_notification() == ori / "same.png"


def test_browser_path_is_not_qq_process() -> None:
    assert QqWindowImageReader._is_qq_process_path(r"C:\Program Files\Google\Chrome\Application\chrome.exe") is False
    assert QqWindowImageReader._is_qq_process_path(r"C:\Program Files\Tencent\QQNT\QQ.exe") is True
