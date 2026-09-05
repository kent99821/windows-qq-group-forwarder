from __future__ import annotations

from dataclasses import dataclass
import glob
import hashlib
import logging
import os
from pathlib import Path
import re
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
CACHE_FILENAME_RE = re.compile(
    r"^(?P<hash>[0-9a-f]{32})(?P<variant>_(?:0|720))?(?P<extension>\.(?:jpg|jpeg|png|gif|webp|bmp))$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ImageCandidate:
    path: Path
    mtime: float
    size: int
    cache_key: str
    cache_kind: str


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

    Windows 通知本身不带原图。这里仅接受通知前后新建或发生变化、且符合
    QQ NT ``Pic/<月份>/{Ori,Thumb}`` 命名规则并通过图片文件头校验的文件。
    同一哈希的原图和缩略图视为同一条图片；原图优先，剩余候选按进入缓存
    的时间顺序取出，避免连续图片先发后一张。
    """

    def __init__(self, config: SourceConfig) -> None:
        self.config = config
        configured = config.image_cache_paths or tuple(_default_cache_roots())
        self.roots = [path for path in _expand_paths(configured) if path.exists()]
        self._known: dict[str, tuple[int, int]] = {}
        self._originals: dict[str, ImageCandidate] = {}
        # 一次扫描可能得到同一条图片的原图和缩略图，或连续多条图片的
        # 多个原图。未被本次调用选中的候选必须保留给后续图片消息。
        self._pending: dict[str, ImageCandidate] = {}
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

    @staticmethod
    def _cache_identity(path: Path) -> tuple[str, str] | None:
        """解析 QQ NT 缓存文件名，排除 Temp 和普通图片文件。"""
        directory = path.parent.name.casefold()
        if directory == "ori":
            expected_kind = "original"
        elif directory == "thumb":
            expected_kind = "thumbnail"
        else:
            return None
        match = CACHE_FILENAME_RE.fullmatch(path.name)
        if match is None:
            return None
        variant = match.group("variant") or ""
        if expected_kind == "original" and variant:
            return None
        if expected_kind == "thumbnail" and not variant:
            return None
        return match.group("hash").casefold(), expected_kind

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
                    cache_identity = self._cache_identity(path)
                    if cache_identity is None:
                        continue
                    if stat.st_mtime < lower_bound or stat.st_mtime > upper_bound:
                        continue
                    if not self._is_image(path):
                        continue
                    cache_key, cache_kind = cache_identity
                    candidate = ImageCandidate(path, stat.st_mtime, stat.st_size, cache_key, cache_kind)
                    candidates.append(candidate)
                    if cache_kind == "original" and self._prefer_candidate(candidate, self._originals.get(cache_key)):
                        self._originals[cache_key] = candidate
            except OSError:
                continue
        return candidates

    def _refresh_original_index(self) -> None:
        """建立全量 Ori 索引，用于关联历史原图和本次新到的缩略图。"""
        for root in self.roots:
            try:
                for path in root.rglob("*"):
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    if not path.is_file() or stat.st_size <= 128:
                        continue
                    cache_identity = self._cache_identity(path)
                    if cache_identity is None or cache_identity[1] != "original":
                        continue
                    if not self._is_image(path):
                        continue
                    cache_key, cache_kind = cache_identity
                    candidate = ImageCandidate(path, stat.st_mtime, stat.st_size, cache_key, cache_kind)
                    if self._prefer_candidate(candidate, self._originals.get(cache_key)):
                        self._originals[cache_key] = candidate
            except OSError:
                continue

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

    @staticmethod
    def _prefer_candidate(candidate: ImageCandidate, current: ImageCandidate | None) -> bool:
        if current is None:
            return True
        # 同一哈希的 Ori 和 Thumb 只保留一个候选，Ori 永远优先；同类文件
        # 选择较新的版本，避免把正在写入的旧文件覆盖掉。
        candidate_rank = candidate.cache_kind == "original"
        current_rank = current.cache_kind == "original"
        if candidate_rank != current_rank:
            return candidate_rank
        return (candidate.mtime, candidate.size, str(candidate.path).casefold()) > (
            current.mtime,
            current.size,
            str(current.path).casefold(),
        )

    def _remember(self, candidates: Iterable[ImageCandidate]) -> None:
        for candidate in candidates:
            # QQ 可能先写 Thumb、后写 Ori，也可能 Ori 早已存在而只重新
            # 生成 Thumb。按共享哈希关联到原图，避免把同一张图当成两条。
            if candidate.cache_kind == "thumbnail":
                candidate = self._originals.get(candidate.cache_key, candidate)
            current = self._pending.get(candidate.cache_key)
            if self._prefer_candidate(candidate, current):
                self._pending[candidate.cache_key] = candidate

    def prime(self) -> None:
        """建立启动基线，避免把已有缓存图片当成新消息。"""
        self._refresh_original_index()
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
            self._remember(candidates)
            if self._pending:
                pending = list(self._pending.values())
                original_candidates = [item for item in pending if item.cache_kind == "original"]
                pool = original_candidates or pending
                # 通知聚合时多个文件可能在同一次扫描中出现。按缓存写入
                # 先后消费，而不是按大小/最新时间逆序消费。
                pool.sort(key=lambda item: (item.mtime, -item.size, str(item.path).casefold()))
                selected = pool[0]
                self._pending.pop(selected.cache_key, None)
                LOGGER.info(
                    "匹配到 QQ 图片缓存 path=%s kind=%s hash=%s size=%d mtime=%.3f age_seconds=%.3f candidates=%d",
                    selected.path,
                    selected.cache_kind,
                    selected.cache_key,
                    selected.size,
                    selected.mtime,
                    max(0.0, time.time() - selected.mtime),
                    len(pending),
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
                {
                    "path": str(item.path),
                    "size": item.size,
                    "mtime": item.mtime,
                    "cache_key": item.cache_key,
                    "cache_kind": item.cache_kind,
                }
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
