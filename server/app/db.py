"""SQLite 元数据仓储(design.md 第 5 节,选型 T3)。

仅存清理追溯所需最小集(rooms / videos);成员与播放状态不落库
(需求 D3)。抽象接口便于测试注入内存假实现;生产实现为
SqliteRoomRepository / SqliteVideoRepository(标准库 sqlite3 + WAL,
阻塞调用经 asyncio.to_thread 下放,design.md 4.6)。

依赖方向:db → models(仅作参数类型)。
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from abc import ABC, abstractmethod
from pathlib import Path

from server.app.models import VideoMeta

# 结构版本号(写 PRAGMA user_version;表结构变更时递增)
SCHEMA_VERSION = 2  # v2:rooms 增加 password_hash(N3,2026-11-15)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS rooms (
    room_id       TEXT PRIMARY KEY,
    created_at    REAL NOT NULL,           -- unix 秒
    host_user_id  TEXT NOT NULL,           -- 仅追溯用,重启不恢复
    password_hash TEXT                     -- 房间密码哈希(N3);NULL = 无密码
);

CREATE TABLE IF NOT EXISTS videos (
    video_id      TEXT PRIMARY KEY,        -- 即存储文件名主干
    room_id       TEXT NOT NULL,
    original_name TEXT NOT NULL,
    size          INTEGER NOT NULL,
    created_at    REAL NOT NULL,
    file_path     TEXT NOT NULL            -- 相对 data 目录
);

CREATE INDEX IF NOT EXISTS idx_videos_room ON videos(room_id);
"""


class SqliteDatabase:
    """共享 SQLite 连接:WAL 模式 + 线程锁(design.md 第 5 节连接参数)。

    - check_same_thread=False:允许 asyncio.to_thread 工作线程复用连接;
    - threading.Lock:线程池可能并发执行同步操作,锁内串行;
    - 仓储类内部持有本对象,业务代码不直接访问 conn。
    """

    def __init__(self, db_path: Path) -> None:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                # 结构变更:重启后内存房间为空,旧数据本就属于待清理残留
                # (需求 4.5),直接重建(降级/升级均按此处理)。
                self._conn.executescript("DROP TABLE IF EXISTS rooms; DROP TABLE IF EXISTS videos;")
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.commit()

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class RoomRepository(ABC):
    """rooms 表仓储。"""

    @abstractmethod
    async def insert_room(
        self,
        room_id: str,
        host_user_id: str,
        created_at: float,
        password_hash: str | None = None,
    ) -> None:
        """创建房间时写入(读写时机表:design.md 第 5 节)。"""

    @abstractmethod
    async def delete_room(self, room_id: str) -> None:
        """房间过期/解散/启动清理时删除(幂等)。"""

    @abstractmethod
    async def delete_all_rooms(self) -> int:
        """启动清理:删除全部房间行,返回删除数(design.md 4.9)。"""


class SqliteRoomRepository(RoomRepository):
    """rooms 表 SQLite 实现。"""

    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def insert_room(
        self,
        room_id: str,
        host_user_id: str,
        created_at: float,
        password_hash: str | None = None,
    ) -> None:
        await asyncio.to_thread(self._insert_room, room_id, host_user_id, created_at, password_hash)

    def _insert_room(
        self,
        room_id: str,
        host_user_id: str,
        created_at: float,
        password_hash: str | None,
    ) -> None:
        with self._db.lock:
            # 主键冲突时覆盖(room_id 生成端有去重重试,此处兜底不崩溃)
            self._db.conn.execute(
                "INSERT OR REPLACE INTO rooms (room_id, host_user_id, created_at, password_hash) "
                "VALUES (?, ?, ?, ?)",
                (room_id, host_user_id, created_at, password_hash),
            )
            self._db.conn.commit()

    async def delete_room(self, room_id: str) -> None:
        await asyncio.to_thread(self._delete_room, room_id)

    def _delete_room(self, room_id: str) -> None:
        with self._db.lock:
            self._db.conn.execute("DELETE FROM rooms WHERE room_id = ?", (room_id,))
            self._db.conn.commit()

    async def delete_all_rooms(self) -> int:
        return await asyncio.to_thread(self._delete_all_rooms)

    def _delete_all_rooms(self) -> int:
        with self._db.lock:
            cur = self._db.conn.execute("DELETE FROM rooms")
            self._db.conn.commit()
            return cur.rowcount


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


class SqliteVideoRepository(VideoRepository):
    """videos 表 SQLite 实现。"""

    def __init__(self, database: SqliteDatabase) -> None:
        self._db = database

    async def insert_video(
        self,
        video: VideoMeta,
        room_id: str,
        file_path: Path,
        created_at: float,
    ) -> None:
        await asyncio.to_thread(
            self._insert_video,
            video.video_id,
            video.original_name,
            video.size,
            room_id,
            created_at,
            Path(file_path).as_posix(),
        )

    def _insert_video(
        self,
        video_id: str,
        original_name: str,
        size: int,
        room_id: str,
        created_at: float,
        file_path: str,
    ) -> None:
        with self._db.lock:
            self._db.conn.execute(
                "INSERT OR REPLACE INTO videos "
                "(video_id, room_id, original_name, size, created_at, file_path) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (video_id, room_id, original_name, size, created_at, file_path),
            )
            self._db.conn.commit()

    async def delete_video(self, video_id: str) -> None:
        await asyncio.to_thread(self._delete_video, video_id)

    def _delete_video(self, video_id: str) -> None:
        with self._db.lock:
            self._db.conn.execute("DELETE FROM videos WHERE video_id = ?", (video_id,))
            self._db.conn.commit()

    async def list_by_room(self, room_id: str) -> list[tuple[VideoMeta, Path]]:
        return await asyncio.to_thread(self._list_by_room, room_id)

    def _list_by_room(self, room_id: str) -> list[tuple[VideoMeta, Path]]:
        with self._db.lock:
            rows = self._db.conn.execute(
                "SELECT video_id, original_name, size, file_path FROM videos WHERE room_id = ?",
                (room_id,),
            ).fetchall()
        return [(VideoMeta(r[0], r[1], r[2]), Path(r[3])) for r in rows]

    async def list_all(self) -> list[tuple[VideoMeta, Path]]:
        return await asyncio.to_thread(self._list_all)

    def _list_all(self) -> list[tuple[VideoMeta, Path]]:
        with self._db.lock:
            rows = self._db.conn.execute(
                "SELECT video_id, original_name, size, file_path FROM videos"
            ).fetchall()
        return [(VideoMeta(r[0], r[1], r[2]), Path(r[3])) for r in rows]

    async def delete_all_videos(self) -> int:
        return await asyncio.to_thread(self._delete_all_videos)

    def _delete_all_videos(self) -> int:
        with self._db.lock:
            cur = self._db.conn.execute("DELETE FROM videos")
            self._db.conn.commit()
            return cur.rowcount
