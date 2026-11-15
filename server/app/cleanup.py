"""后台清理任务(design.md 4.9)。

职责:
  - startup_cleanup(进程启动):内存房间为空,DB 中全部 rooms/videos
    行均视为残留 → 先删物理文件再删行(需求 4.5:重启后房间失效);
  - run 周期扫描(间隔 cleanup.scan_interval_seconds),每轮 scan_once:
    1. 空房过期:RoomManager.purge_expired_rooms(需求 3.1.7);
    2. 上传临时文件:storage.sweep_tmp(超龄兜底);
    3. 孤儿文件兜底:videos/ 目录存在但 DB 无记录的文件 → 删除。

依赖方向:cleanup → room_manager / video_storage / db / config。
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress

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
        """启动清理:全部 DB 行对应物理文件与行删除(需求 4.5,design.md 4.9)。

        先删物理文件再删行(第 5 节读写时机表);房间行随后全删。
        """
        for meta, _ in await self._video_repo.list_all():
            await self._storage.delete(meta.video_id)
        await self._video_repo.delete_all_videos()
        await self._room_repo.delete_all_rooms()

    async def scan_once(self) -> None:
        """单轮扫描:空房过期 → 临时文件 → 孤儿文件(可测试直接调用)。"""
        # 1. 空房过期(需求 3.1.7)
        await self._room_manager.purge_expired_rooms(now=time.time())
        # 2. 上传临时文件超龄兜底(design.md 4.9 第 2 条)
        await self._storage.sweep_tmp(self._settings.cleanup.temp_file_max_age_seconds)
        # 3. 孤儿文件兜底:videos/ 存在但 DB 无记录(design.md 4.9 第 3 条)
        known = {meta.video_id for meta, _ in await self._video_repo.list_all()}
        for video_id in await asyncio.to_thread(self._storage.list_video_ids):
            if video_id not in known:
                await self._storage.delete(video_id)

    async def run(self) -> None:
        """周期扫描主循环;被取消时正常退出。"""
        interval = self._settings.cleanup.scan_interval_seconds
        while True:
            await asyncio.sleep(interval)
            await self.scan_once()

    def start(self) -> None:
        """以 asyncio.create_task 启动周期任务(lifespan 启动时调用)。"""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        """取消任务并等待退出(lifespan shutdown 调用,幂等)。"""
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
