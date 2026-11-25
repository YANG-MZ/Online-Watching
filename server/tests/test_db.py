"""SQLite 仓储测试(design.md 第 5 节)。"""

import sqlite3
from pathlib import Path

import pytest

from server.app.db import SCHEMA_VERSION, SqliteDatabase, SqliteRoomRepository, SqliteVideoRepository
from server.app.models import VideoMeta


@pytest.fixture
def db(tmp_path) -> SqliteDatabase:
    database = SqliteDatabase(tmp_path / "data" / "app.db")
    yield database
    database.close()


@pytest.fixture
def room_repo(db: SqliteDatabase) -> SqliteRoomRepository:
    return SqliteRoomRepository(db)


@pytest.fixture
def video_repo(db: SqliteDatabase) -> SqliteVideoRepository:
    return SqliteVideoRepository(db)


# ---------- schema ----------

def test_schema_and_pragmas(tmp_path) -> None:
    """建库即建表:WAL 开启、user_version 写入、两表与索引存在。"""
    db_path = tmp_path / "data" / "app.db"
    SqliteDatabase(db_path).close()

    raw = sqlite3.connect(str(db_path))
    try:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"rooms", "videos"} <= tables
        indexes = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_videos_room" in indexes
    finally:
        raw.close()


# ---------- rooms ----------

async def test_room_insert_and_delete(room_repo: SqliteRoomRepository) -> None:
    """插入房间;删除幂等(不存在也成功)。"""
    await room_repo.insert_room("ROOM01", "user1", 1000.0)
    await room_repo.insert_room("ROOM02", "user2", 1001.0)

    await room_repo.delete_room("ROOM01")
    await room_repo.delete_room("ROOM01")  # 幂等
    assert await room_repo.delete_all_rooms() == 1  # 仅剩 ROOM02


async def test_delete_all_rooms_count_and_repeat(room_repo: SqliteRoomRepository) -> None:
    """delete_all_rooms 返回删除数;空表再删返回 0。"""
    for i in range(3):
        await room_repo.insert_room(f"ROOM{i}", f"user{i}", 1000.0 + i)
    assert await room_repo.delete_all_rooms() == 3
    assert await room_repo.delete_all_rooms() == 0


async def test_room_insert_replace_same_id(room_repo: SqliteRoomRepository) -> None:
    """同 room_id 重复插入为主键覆盖(生成端有去重,此处兜底不崩溃)。"""
    await room_repo.insert_room("ROOM01", "user1", 1000.0)
    await room_repo.insert_room("ROOM01", "user2", 2000.0)
    assert await room_repo.delete_all_rooms() == 1


# ---------- videos ----------

async def test_video_insert_and_list_by_room(
    video_repo: SqliteVideoRepository,
) -> None:
    """按房间列出视频:元数据 + 相对路径(Path)。"""
    meta = VideoMeta(video_id="vid1", original_name="电影.mp4", size=12345)
    await video_repo.insert_video(meta, "ROOM01", "videos/vid1.mp4", 1000.0)

    rows = await video_repo.list_by_room("ROOM01")
    assert len(rows) == 1
    v, path = rows[0]
    assert v.video_id == "vid1"
    assert v.original_name == "电影.mp4"
    assert v.size == 12345
    assert path == Path("videos/vid1.mp4")

    assert await video_repo.list_by_room("ROOM02") == []


async def test_video_list_all_and_delete_all(
    video_repo: SqliteVideoRepository,
) -> None:
    """list_all 跨房间;delete_all_videos 返回删除数。"""
    for i in range(3):
        await video_repo.insert_video(
            VideoMeta(video_id=f"vid{i}", original_name=f"a{i}.mp4", size=i),
            f"ROOM{i % 2}",
            f"videos/vid{i}.mp4",
            1000.0 + i,
        )
    assert len(await video_repo.list_all()) == 3
    assert await video_repo.delete_all_videos() == 3
    assert await video_repo.list_all() == []


async def test_delete_video_idempotent(video_repo: SqliteVideoRepository) -> None:
    """删除单个视频行幂等。"""
    await video_repo.insert_video(
        VideoMeta(video_id="vid1", original_name="a.mp4", size=1),
        "ROOM01",
        "videos/vid1.mp4",
        1000.0,
    )
    await video_repo.delete_video("vid1")
    await video_repo.delete_video("vid1")  # 幂等,不抛异常
    assert await video_repo.list_by_room("ROOM01") == []


async def test_video_replace_same_id(
    video_repo: SqliteVideoRepository,
) -> None:
    """同 video_id 重复写入覆盖(配合旧行删除流程兜底)。"""
    await video_repo.insert_video(
        VideoMeta(video_id="vid1", original_name="old.mp4", size=1),
        "ROOM01",
        "videos/vid1.mp4",
        1000.0,
    )
    await video_repo.insert_video(
        VideoMeta(video_id="vid1", original_name="new.mp4", size=2),
        "ROOM01",
        "videos/vid1.mp4",
        2000.0,
    )
    rows = await video_repo.list_by_room("ROOM01")
    assert len(rows) == 1
    assert rows[0][0].original_name == "new.mp4"


# ---------- schema v2 迁移与密码哈希列(N3) ----------

def test_schema_migration_from_v1(tmp_path) -> None:
    """旧 v1 库:打开后重建为 v2(rooms 带密码列),数据按残留清空。"""
    db_path = tmp_path / "data" / "app.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(db_path))
    raw.executescript(
        "CREATE TABLE rooms (room_id TEXT PRIMARY KEY, created_at REAL, host_user_id TEXT);"
        "CREATE TABLE videos (video_id TEXT PRIMARY KEY);"
        "INSERT INTO rooms VALUES ('old', 1.0, 'u1');"
    )
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()

    db = SqliteDatabase(db_path)
    try:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(rooms)")}
        assert "password_hash" in cols
        assert db.conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0  # 残留清空
    finally:
        db.close()


async def test_room_password_hash_persisted(tmp_path) -> None:
    """password_hash 写入并读取(哈希存储,不落明文)。"""
    db = SqliteDatabase(tmp_path / "data" / "app.db")
    repo = SqliteRoomRepository(db)
    try:
        await repo.insert_room("ROOMPW", "u1", 1000.0, "a" * 64)
        row = db.conn.execute(
            "SELECT password_hash FROM rooms WHERE room_id = 'ROOMPW'"
        ).fetchone()
        assert row[0] == "a" * 64
    finally:
        db.close()
