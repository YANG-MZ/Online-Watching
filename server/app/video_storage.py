"""视频文件存储(design.md 4.7/4.8)。

职责:上传临时文件写盘与原子重命名、Range 分块读取、文件删除、
临时文件超龄清理。文件布局(design.md 第 7 节 storage.data_dir):

    data/
    ├── videos/{video_id}.mp4     # 正式文件(UUID 命名,需求 3.3)
    └── tmp/{upload_id}.part      # 上传中临时文件(中断即删,超龄兜底)

抽象接口便于测试注入内存/临时目录假实现;生产实现为 LocalVideoStorage。

依赖方向:video_storage → models(仅作类型)。
"""

from __future__ import annotations

import asyncio
import os
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from pathlib import Path

# 拉流分块大小(design.md 4.8:1 MiB/块异步输出)
CHUNK_SIZE = 1024 * 1024
# 上传中止复查间隔(design.md 4.7 步骤 6:每写 64 MiB 复查一次)
DEFAULT_ABORT_CHECK_INTERVAL = 64 * 1024 * 1024


class VideoStorage(ABC):
    """本地文件系统视频存储。"""

    @abstractmethod
    def ensure_dirs(self) -> None:
        """确保 data/videos、data/tmp 目录存在(启动时调用)。"""

    @abstractmethod
    def video_path(self, video_id: str) -> Path:
        """返回正式文件完整路径 data/videos/{video_id}.mp4。"""

    @abstractmethod
    async def write_upload(
        self,
        upload_id: str,
        stream: AsyncIterator[bytes],
        abort_check: Callable[[], bool] | None = None,
    ) -> int:
        """将上传字节流式写入 data/tmp/{upload_id}.part。

        每累计写满 abort_check_interval 字节调用一次 abort_check(在
        工作线程执行),返回 True 时中止上传(design.md 4.7 步骤 6:
        复查请求发起者仍为房主且房间未解散,回调由 api 层注入,
        存储层不感知房间语义)。

        Returns:
            写入的总字节数。

        Raises:
            UploadSizeExceededError: 累计字节数超过上限(design.md 4.7 步骤 5)。
            UploadAbortedError: 上传被服务端主动中止(abort_check 返回 True)。
        失败(含异常)时临时文件一律删除。
        """

    @abstractmethod
    async def promote_upload(self, upload_id: str, video_id: str) -> Path:
        """上传完成:fsync(写入末尾已完成)后原子重命名
        tmp/{upload_id}.part → videos/{video_id}.mp4。"""

    @abstractmethod
    async def abort_upload(self, upload_id: str) -> None:
        """上传失败/中断:删除临时文件(幂等)。"""

    @abstractmethod
    def stat_size(self, video_id: str) -> int | None:
        """返回正式文件大小;文件不存在返回 None。"""

    @abstractmethod
    def open_range(self, video_id: str, start: int, length: int) -> AsyncIterator[bytes]:
        """按 [start, start+length) 分块(1 MiB)异步读取文件,供 206 响应(design.md 4.8)。"""

    @abstractmethod
    async def delete(self, video_id: str) -> None:
        """删除正式文件(替换/清理,幂等:文件不存在视为成功)。"""

    @abstractmethod
    async def sweep_tmp(self, max_age_seconds: float) -> int:
        """删除超龄临时文件,返回删除数(design.md 4.9 第 2 条)。"""


class LocalVideoStorage(VideoStorage):
    """VideoStorage 的本地文件系统实现。"""

    def __init__(
        self,
        data_dir: Path,
        max_upload_bytes: int,
        abort_check_interval_bytes: int = DEFAULT_ABORT_CHECK_INTERVAL,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._videos_dir = self._data_dir / "videos"
        self._tmp_dir = self._data_dir / "tmp"
        self._max_upload_bytes = max_upload_bytes
        self._abort_interval = abort_check_interval_bytes

    # ---------- 目录与路径 ----------

    def ensure_dirs(self) -> None:
        self._videos_dir.mkdir(parents=True, exist_ok=True)
        self._tmp_dir.mkdir(parents=True, exist_ok=True)

    def video_path(self, video_id: str) -> Path:
        return self._videos_dir / f"{video_id}.mp4"

    def tmp_path(self, upload_id: str) -> Path:
        return self._tmp_dir / f"{upload_id}.part"

    # ---------- 上传写盘(design.md 4.7) ----------

    async def write_upload(
        self,
        upload_id: str,
        stream: AsyncIterator[bytes],
        abort_check: Callable[[], bool] | None = None,
    ) -> int:
        tmp = self.tmp_path(upload_id)
        f = await asyncio.to_thread(self._open_tmp, tmp)
        total = 0
        next_check = self._abort_interval
        try:
            async for chunk in stream:
                total += len(chunk)
                if total > self._max_upload_bytes:
                    raise UploadSizeExceededError(
                        f"上传累计 {total} 字节超过上限 {self._max_upload_bytes}"
                    )
                if total >= next_check:
                    next_check = total + self._abort_interval
                    if abort_check is not None and await asyncio.to_thread(abort_check):
                        raise UploadAbortedError("上传被服务端中止")
                await asyncio.to_thread(f.write, chunk)
            await asyncio.to_thread(f.flush)
            await asyncio.to_thread(os.fsync, f.fileno())
        except BaseException:
            await asyncio.to_thread(f.close)
            await asyncio.to_thread(self._unlink_quiet, tmp)
            raise
        await asyncio.to_thread(f.close)
        return total

    @staticmethod
    def _open_tmp(tmp: Path):
        tmp.parent.mkdir(parents=True, exist_ok=True)
        return tmp.open("wb")

    @staticmethod
    def _unlink_quiet(path: Path) -> None:
        path.unlink(missing_ok=True)

    async def promote_upload(self, upload_id: str, video_id: str) -> Path:
        tmp = self.tmp_path(upload_id)
        dest = self.video_path(video_id)

        def _promote() -> None:
            if not tmp.is_file():
                raise VideoStorageError(f"上传临时文件不存在: {tmp.name}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            # 同卷内 os.replace 为原子操作(design.md 4.7 步骤 7)
            os.replace(tmp, dest)

        await asyncio.to_thread(_promote)
        return dest

    async def abort_upload(self, upload_id: str) -> None:
        await asyncio.to_thread(self._unlink_quiet, self.tmp_path(upload_id))

    # ---------- 下发与清理 ----------

    def stat_size(self, video_id: str) -> int | None:
        try:
            return self.video_path(video_id).stat().st_size
        except FileNotFoundError:
            return None

    async def open_range(self, video_id: str, start: int, length: int) -> AsyncIterator[bytes]:
        path = self.video_path(video_id)

        def _read(f, size: int) -> bytes:
            return f.read(size)

        with path.open("rb") as f:
            await asyncio.to_thread(f.seek, start)
            remaining = length
            while remaining > 0:
                data = await asyncio.to_thread(_read, f, min(CHUNK_SIZE, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    async def delete(self, video_id: str) -> None:
        await asyncio.to_thread(self._unlink_quiet, self.video_path(video_id))

    async def sweep_tmp(self, max_age_seconds: float) -> int:
        def _sweep() -> int:
            removed = 0
            if not self._tmp_dir.is_dir():
                return 0
            now = time.time()
            for p in self._tmp_dir.glob("*.part"):
                try:
                    if now - p.stat().st_mtime > max_age_seconds:
                        p.unlink(missing_ok=True)
                        removed += 1
                except FileNotFoundError:
                    continue
            return removed

        return await asyncio.to_thread(_sweep)


class VideoStorageError(Exception):
    """存储层错误基类。"""


class UploadSizeExceededError(VideoStorageError):
    """上传累计字节超过 max_upload_bytes(需求 3.3 / 验收 11)。"""


class UploadAbortedError(VideoStorageError):
    """上传被服务端主动中止(design.md 4.7 步骤 6:房主变更/房间解散)。"""
