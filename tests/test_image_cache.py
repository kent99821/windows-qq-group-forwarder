import os
from pathlib import Path
import time

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
    cache_path = tmp_path / "nt_data" / "Pic" / "Ori"
    cache_path.mkdir(parents=True)
    cache = QqImageCache(source_config(cache_path))
    cache.prime()

    image = cache_path / "0123456789abcdef0123456789abcdef.jpg"
    image.write_bytes(b"\xff\xd8\xff" + b"x" * 256)
    found = cache.find_for_notification()

    assert found == image


def test_default_cache_settings_allow_delayed_qq_download() -> None:
    config = SourceConfig("发家致富", "QQ", 1.0, ())

    assert config.image_cache_match_seconds >= 60.0
    assert config.image_cache_wait_seconds >= 30.0


def test_keeps_unselected_candidates_for_consecutive_images(tmp_path: Path) -> None:
    cache_path = tmp_path / "Pic" / "Ori"
    cache_path.mkdir(parents=True)
    cache = QqImageCache(source_config(cache_path.parent))
    cache.prime()

    first = cache_path / "0123456789abcdef0123456789abcdef.png"
    second = cache_path / "fedcba9876543210fedcba9876543210.png"
    first.write_bytes(b"\x89PNG\r\n\x1a\n" + b"a" * 256)
    second.write_bytes(b"\x89PNG\r\n\x1a\n" + b"b" * 512)

    found = {cache.find_for_notification(), cache.find_for_notification()}

    assert found == {first, second}


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
    (thumb / "0123456789abcdef0123456789abcdef_0.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"t" * 200)
    (ori / "0123456789abcdef0123456789abcdef.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"o" * 200)
    assert cache.find_for_notification() == ori / "0123456789abcdef0123456789abcdef.png"


def test_uses_cache_layout_and_rejects_temp_or_unrelated_files(tmp_path: Path) -> None:
    cache_path = tmp_path / "Pic"
    ori = cache_path / "2026-09" / "Ori"
    thumb_temp = cache_path / "2026-09" / "ThumbTemp"
    ori.mkdir(parents=True)
    thumb_temp.mkdir()
    valid = ori / "0123456789abcdef0123456789abcdef.png"
    valid.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 256)
    (thumb_temp / "fedcba9876543210fedcba9876543210_0").write_bytes(b"x" * 512)
    (cache_path / "unrelated.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 256)

    cache = QqImageCache(source_config(cache_path))
    cache.prime()
    newer = ori / "fedcba9876543210fedcba9876543210.jpg"
    newer.write_bytes(b"\xff\xd8\xff" + b"y" * 256)

    assert cache.find_for_notification() == newer


def test_returns_candidates_in_cache_arrival_order(tmp_path: Path) -> None:
    cache_path = tmp_path / "Pic" / "Ori"
    cache_path.mkdir(parents=True)
    cache = QqImageCache(source_config(cache_path.parent))
    cache.prime()

    first = cache_path / "0123456789abcdef0123456789abcdef.png"
    second = cache_path / "fedcba9876543210fedcba9876543210.png"
    first.write_bytes(b"\x89PNG\r\n\x1a\n" + b"a" * 256)
    time.sleep(0.02)
    second.write_bytes(b"\x89PNG\r\n\x1a\n" + b"b" * 256)
    now = time.time()
    os.utime(first, (now - 2, now - 2))
    os.utime(second, (now - 1, now - 1))

    assert cache.find_for_notification() == first
    assert cache.find_for_notification() == second


def test_consumes_one_hash_as_one_image_not_ori_and_thumb(tmp_path: Path) -> None:
    cache_path = tmp_path / "Pic"
    ori = cache_path / "2026-09" / "Ori"
    thumb = cache_path / "2026-09" / "Thumb"
    ori.mkdir(parents=True)
    thumb.mkdir()
    cache = QqImageCache(source_config(cache_path))
    cache.prime()

    image_hash = "0123456789abcdef0123456789abcdef"
    ori_file = ori / f"{image_hash}.png"
    thumb_file = thumb / f"{image_hash}_720.png"
    ori_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"o" * 512)
    thumb_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"t" * 256)

    assert cache.find_for_notification() == ori_file
    assert cache.find_for_notification() is None


def test_links_new_thumbnail_to_existing_original(tmp_path: Path) -> None:
    cache_path = tmp_path / "Pic"
    ori = cache_path / "2026-09" / "Ori"
    thumb = cache_path / "2026-09" / "Thumb"
    ori.mkdir(parents=True)
    thumb.mkdir(parents=True)
    image_hash = "fedcba9876543210fedcba9876543210"
    ori_file = ori / f"{image_hash}.jpeg"
    ori_file.write_bytes(b"\xff\xd8\xff" + b"o" * 512)

    cache = QqImageCache(source_config(cache_path))
    cache.prime()
    thumb_file = thumb / f"{image_hash}_720.png"
    thumb_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"t" * 256)

    assert cache.find_for_notification() == ori_file


def test_browser_path_is_not_qq_process() -> None:
    assert QqWindowImageReader._is_qq_process_path(r"C:\Program Files\Google\Chrome\Application\chrome.exe") is False
    assert QqWindowImageReader._is_qq_process_path(r"C:\Program Files\Tencent\QQNT\QQ.exe") is True
