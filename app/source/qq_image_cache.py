from __future__ import annotations

from dataclasses import dataclass
import glob
import hashlib
import logging
import os
from pathlib import Path
import shutil
import time
from typing import Iterable

from ..config import SourceConfig


LOGGER = logging.getLogger(__name__)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
IMAGE_SIGNATURES = (
    b"\xff\xd8\xff",  # JPEG
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"GIF87a",
    b"GIF89a",
    b"RIFF",  # WEBP; checked together with WEBP below
    b"BM",  # BMP
)


@dataclass(frozen=True)
class ImageCandidate:
    path: Path
    mtime: float
    size: int


def _default_cache_roots() -> list[Path]:
    """返回 QQ NT 常见图片缓存位置；不存在的目录会在扫描时忽略。"""
    user_profile = Path(os.environ.get("USERPROFILE", str(Path.home())))
    local_app_data = Path(os.environ.get("LOCALAPPDATA", user_profile / "AppData" / "Local"))
    app_data = Path(os.environ.get("APPDATA", user_profile / "AppData" / "Roaming"))
    documents = user_profile / "Documents" / "Tencent Files"
    roots = [
        local_app_data / "Tencent" / "QQ" / "nt_qq" / "nt_data" / "Pic",
        app_data / "Tencent" / "QQ" / "nt_qq" / "nt_data" / "Pic",
    ]
    if documents.is_dir():
        roots.extend(documents.glob("*/nt_qq/nt_data/Pic"))
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).casefold()
        if key not in seen:
            unique.append(root)
            seen.add(key)
    return unique


def _expand_paths(paths: Iterable[Path]) -> list[Path]:
    expanded: list[Path] = []
    for path in paths:
        raw = os.path.expandvars(str(path))
        matches = glob.glob(raw) if glob.has_magic(raw) else [raw]
        expanded.extend(Path(item).expanduser() for item in matches)
    return expanded


class QqImageCache:
    """从 QQ 本地缓存中寻找刚由通知触发产生的图片文件。

    Windows 通知本身不带原图。这里仅接受通知前后新建或发生变化、且能够
    通过图片文件头校验的文件；发现多个候选时选择最大文件，通常对应原图
    而不是 QQ 生成的缩略图。
    """

    def __init__(self, config: SourceConfig) -> None:
        self.config = config
        configured = config.image_cache_paths or tuple(_default_cache_roots())
        self.roots = [path for path in _expand_paths(configured) if path.exists()]
        self._known: dict[str, tuple[int, int]] = {}
        if self.roots:
            LOGGER.info("QQ 图片缓存探测目录：%s", "; ".join(str(path) for path in self.roots))
        else:
            LOGGER.warning("未找到 QQ 图片缓存目录；图片通知将回退为占位提示")

    @staticmethod
    def _is_image(path: Path) -> bool:
        if path.suffix.casefold() in IMAGE_EXTENSIONS:
            return True
        try:
            with path.open("rb") as handle:
                header = handle.read(12)
        except OSError:
            return False
        if header.startswith(b"RIFF"):
            return len(header) >= 12 and header[8:12] == b"WEBP"
        return any(header.startswith(signature) for signature in IMAGE_SIGNATURES if signature != b"RIFF")

    def _iter_recent_files(self, now: float) -> list[ImageCandidate]:
        candidates: list[ImageCandidate] = []
        lower_bound = now - self.config.image_cache_match_seconds
        upper_bound = now + 3.0
        for root in self.roots:
            try:
                iterator = root.rglob("*")
                for path in iterator:
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    if not path.is_file() or stat.st_size <= 128:
                        continue
                    if stat.st_mtime < lower_bound or stat.st_mtime > upper_bound:
                        continue
                    if not self._is_image(path):
                        continue
                    candidates.append(ImageCandidate(path, stat.st_mtime, stat.st_size))
            except OSError:
                continue
        return candidates

    def _scan(self, now: float) -> list[ImageCandidate]:
        current: dict[str, tuple[int, int]] = {}
        candidates = self._iter_recent_files(now)
        for item in candidates:
            try:
                stat = item.path.stat()
            except OSError:
                continue
            current[str(item.path).casefold()] = (stat.st_mtime_ns, stat.st_size)
        changed = [
            item for item in candidates
            if current.get(str(item.path).casefold()) != self._known.get(str(item.path).casefold())
        ]
        self._known.update(current)
        return changed

    def prime(self) -> None:
        """建立启动基线，避免把已有缓存图片当成新消息。"""
        now = time.time()
        self._scan(now)

    def find_for_notification(self) -> Path | None:
        """返回与最近图片通知对应的候选原图，无法安全判断时返回 None。"""
        if not self.roots:
            return None
        settle = self.config.image_cache_settle_seconds
        if settle:
            time.sleep(settle)
        deadline = time.monotonic() + self.config.image_cache_wait_seconds
        while True:
            candidates = self._scan(time.time())
            if candidates:
                # QQ NT 通常把原图放在 Ori、缩略图放在 Thumb；优先 Ori，
                # 同一目录仍按时间和大小选择最新/最大的候选。
                original_candidates = [
                    item for item in candidates
                    if any(part.casefold() == "ori" for part in item.path.parts)
                ]
                pool = original_candidates or candidates
                pool.sort(key=lambda item: (item.mtime, item.size), reverse=True)
                selected = pool[0]
                LOGGER.info(
                    "匹配到 QQ 图片缓存 path=%s size=%d candidates=%d",
                    selected.path,
                    selected.size,
                    len(candidates),
                )
                return selected.path
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                LOGGER.warning(
                    "图片通知已收到，但 QQ 缓存中未发现近期图片文件 wait_seconds=%.1f",
                    self.config.image_cache_wait_seconds,
                )
                return None
            time.sleep(min(0.5, remaining))

    def inspect(self) -> dict[str, object]:
        """提供不改变基线的本地探测结果，便于首次配置时校准目录。"""
        candidates = self._iter_recent_files(time.time())
        return {
            "roots": [str(path) for path in self.roots],
            "recent_images": [
                {"path": str(item.path), "size": item.size, "mtime": item.mtime}
                for item in sorted(candidates, key=lambda item: item.mtime, reverse=True)
            ],
        }

    def stage(self, source: Path, message_key: str, directory: Path) -> Path:
        """将缓存图片复制到队列专用目录，保证重试期间源缓存变化不影响发送。"""
        if not source.is_file() or not self._is_image(source):
            raise RuntimeError(f"QQ 图片缓存文件不可读：{source}")
        directory.mkdir(parents=True, exist_ok=True)
        suffix = source.suffix.lower() if source.suffix.lower() in IMAGE_EXTENSIONS else ".bin"
        safe_key = hashlib.sha256(message_key.encode("utf-8")).hexdigest()[:24]
        target = directory / f"{safe_key}{suffix}"
        shutil.copy2(source, target)
        return target
