"""视频文件存储抽象(design.md 4.7/4.8)。

职责:上传临时文件写盘与原子重命名、Range 分块读取、文件删除、
临时文件超龄清理。文件布局(design.md 第 7 节 storage.data_dir):

    data/
    ├── videos/{video_id}.mp4     # 正式文件(UUID 命名,需求 3.3)
    └── tmp/{upload_id}.part      # 上传中临时文件(中断即删,超龄兜底)

抽象接口便于测试注入内存/临时目录假实现;生产实现为
LocalVideoStorage。依赖方向:video_storage → models(仅作类型)。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from pathlib import Path


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
    ) -> int:
        """将上传字节流式写入 data/tmp/{upload_id}.part。

        Returns:
            写入的总字节数。

        Raises:
            UploadSizeExceededError: 累计字节数超过上限(design.md 4.7 步骤 5)。
            UploadAbortedError: 上传被中止(房主变更/房间解散的中途复查)。
        """

    @abstractmethod
    async def promote_upload(self, upload_id: str, video_id: str) -> Path:
        """上传完成:fsync 后原子重命名 tmp/{upload_id}.part → videos/{video_id}.mp4。"""

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


class VideoStorageError(Exception):
    """存储层错误基类。"""


class UploadSizeExceededError(VideoStorageError):
    """上传累计字节超过 max_upload_bytes(需求 3.3 / 验收 11)。"""


class UploadAbortedError(VideoStorageError):
    """上传被服务端主动中止(design.md 4.7 步骤 6:房主变更/房间解散)。"""
