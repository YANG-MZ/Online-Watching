"""SQLite 元数据仓储抽象(design.md 第 5 节,选型 T3)。

仅存清理追溯所需最小集(rooms / videos);成员与播放状态不落库
(需求 D3)。抽象接口便于测试注入内存假实现;生产实现为
SqliteRoomRepository / SqliteVideoRepository(标准库 sqlite3 + WAL,
阻塞调用经 asyncio.to_thread 下放,design.md 4.6)。

依赖方向:db → models(仅作参数类型)。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from server.app.models import VideoMeta


class RoomRepository(ABC):
    """rooms 表仓储。"""

    @abstractmethod
    async def insert_room(self, room_id: str, host_user_id: str, created_at: float) -> None:
        """创建房间时写入(读写时机表:design.md 第 5 节)。"""

    @abstractmethod
    async def delete_room(self, room_id: str) -> None:
        """房间过期/解散/启动清理时删除(幂等)。"""

    @abstractmethod
    async def delete_all_rooms(self) -> int:
        """启动清理:删除全部房间行,返回删除数(design.md 4.9)。"""


class VideoRepository(ABC):
    """videos 表仓储。file_path 为相对 data 目录的路径。"""

    @abstractmethod
    async def insert_video(
        self,
        video: VideoMeta,
        room_id: str,
        file_path: Path,
        created_at: float,
    ) -> None:
        """上传完成时写入;替换场景由实现保证旧行删除与新增的原子顺序。"""

    @abstractmethod
    async def delete_video(self, video_id: str) -> None:
        """删除单个视频行(替换/清理,幂等)。"""

    @abstractmethod
    async def list_by_room(self, room_id: str) -> list[tuple[VideoMeta, Path]]:
        """列出房间名下全部视频(含文件路径),清理房间时使用。"""

    @abstractmethod
    async def list_all(self) -> list[tuple[VideoMeta, Path]]:
        """列出全部视频(启动清理/孤儿文件核对)。"""

    @abstractmethod
    async def delete_all_videos(self) -> int:
        """启动清理:删除全部视频行,返回删除数。"""
