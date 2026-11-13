"""后台清理任务(design.md 4.9)。

本阶段仅定义任务签名,业务逻辑后续实现。

职责(单协程,周期 cleanup.scan_interval_seconds):
  1. 空房过期:empty_since 超过 room.empty_expire_seconds →
     删视频文件 + SQLite 行 + 内存房间(需求 3.1.7);
  2. 上传临时文件:storage.sweep_tmp(temp_file_max_age_seconds);
  3. 孤儿文件兜底:videos/ 目录存在但 DB 无记录的文件 → 删除。

启动清理(进程启动,design.md 4.9):内存房间为空,DB 中全部
rooms/videos 行均视为残留 → 先删物理文件再删行(需求 4.5)。

依赖方向:cleanup → room_manager / video_storage / db / config。
"""

from __future__ import annotations

import asyncio

from server.app.config import Settings
from server.app.db import RoomRepository, VideoRepository
from server.app.room_manager import RoomManager
from server.app.video_storage import VideoStorage


class CleanupTask:
    """后台周期清理任务(lifespan 启动/取消)。"""

    def __init__(
        self,
        settings: Settings,
        room_manager: RoomManager,
        storage: VideoStorage,
        room_repo: RoomRepository,
        video_repo: VideoRepository,
    ) -> None:
        self._settings = settings
        self._room_manager = room_manager
        self._storage = storage
        self._room_repo = room_repo
        self._video_repo = video_repo
        self._task: asyncio.Task | None = None

    async def startup_cleanup(self) -> None:
        """启动清理:删除全部 DB 行对应的物理文件与行(需求 4.5)。"""

    async def run(self) -> None:
        """周期扫描主循环(由 start 调度;取消时正常退出)。"""

    def start(self) -> None:
        """以 asyncio.create_task 启动周期任务。"""

    async def stop(self) -> None:
        """取消任务并等待退出(lifespan shutdown 调用)。"""
