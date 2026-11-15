"""后台清理任务测试(design.md 4.9)。"""

import asyncio
import os
import time
from pathlib import Path

from server.app.cleanup import CleanupTask
from server.app.config import CleanupSettings
from server.tests.conftest import put_video_file


async def test_startup_cleanup_removes_all(p3_env) -> None:
    """启动清理:DB 全部 rooms/videos 行对应文件与行删除(需求 4.5)。"""
    ctx = p3_env
    await ctx.room_repo.insert_room("R1", "u1", 1.0)
    await put_video_file(ctx, "v1", b"abcd")
    await put_video_file(ctx, "v2", b"efgh")
    assert ctx.storage.stat_size("v1") == 4

    task = CleanupTask(ctx.settings, ctx.manager, ctx.storage, ctx.room_repo, ctx.video_repo)
    await task.startup_cleanup()

    assert ctx.storage.stat_size("v1") is None
    assert ctx.storage.stat_size("v2") is None
    assert await ctx.video_repo.list_all() == []
    assert await ctx.room_repo.delete_all_rooms() == 0  # 已空


async def test_scan_once_purges_expired_room(p3_env) -> None:
    """空房超时销毁:内存房间 + 视频文件 + DB 行(需求 3.1.7,验收 10)。"""
    ctx = p3_env
    cred = await ctx.manager.create_room("Alice", now=1000.0)
    m = ctx.manager
    m.begin_upload(cred.room_id, cred.session_token)
    await ctx.storage.write_upload("up1", _chunks(b"data"))
    await ctx.storage.promote_upload("up1", "vid1")
    from server.app.models import VideoMeta

    await m.complete_upload(
        cred.room_id,
        cred.session_token,
        VideoMeta(video_id="vid1", original_name="v.mp4", size=4),
        Path("videos/vid1.mp4"),
        now=1001.0,
    )
    # 模拟全员离开(倒计时起点拨到 10 分钟前)
    room = m.get_room(cred.room_id)
    room.empty_since = time.time() - 601.0

    task = CleanupTask(ctx.settings, m, ctx.storage, ctx.room_repo, ctx.video_repo)
    await task.scan_once()

    assert m.get_room(cred.room_id) is None
    assert ctx.storage.stat_size("vid1") is None
    assert ctx.db.conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0


async def test_scan_once_sweeps_tmp_and_orphans(p3_env) -> None:
    """临时文件超龄清理 + 孤儿文件兜底;DB 登记的正式文件保留。"""
    ctx = p3_env
    # 正式文件:DB 有记录 → 保留
    await put_video_file(ctx, "keep1", b"keep")
    # 孤儿文件:videos/ 存在但 DB 无记录 → 删除
    orphan_path = ctx.storage.video_path("orphan1")
    orphan_path.parent.mkdir(parents=True, exist_ok=True)
    orphan_path.write_bytes(b"orphan")
    # 超龄临时文件 → 删除
    ctx.storage.ensure_dirs()
    stale_tmp = ctx.storage.tmp_path("stale")
    stale_tmp.write_bytes(b"stale")
    old = time.time() - 7200
    os.utime(stale_tmp, (old, old))
    # 未超龄临时文件 → 保留
    fresh_tmp = ctx.storage.tmp_path("fresh")
    fresh_tmp.write_bytes(b"fresh")

    task = CleanupTask(ctx.settings, ctx.manager, ctx.storage, ctx.room_repo, ctx.video_repo)
    await task.scan_once()

    assert ctx.storage.stat_size("keep1") == 4
    assert not orphan_path.exists()
    assert not stale_tmp.exists()
    assert fresh_tmp.is_file()


async def test_start_stop_lifecycle(p3_env) -> None:
    """start/stop 生命周期:任务运行中可被正常取消退出(幂等)。"""
    ctx = p3_env
    settings = ctx.settings.model_copy(
        update={"cleanup": CleanupSettings(scan_interval_seconds=0.05)}
    )
    task = CleanupTask(settings, ctx.manager, ctx.storage, ctx.room_repo, ctx.video_repo)

    task.start()
    await asyncio.sleep(0.2)  # 覆盖至少 2 轮扫描
    assert task._task is not None and not task._task.done()

    await task.stop()
    assert task._task is None
    await task.stop()  # 幂等


async def _chunks(*chunks: bytes):
    for c in chunks:
        yield c
