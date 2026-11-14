"""LocalVideoStorage 测试(design.md 4.7/4.8/4.9)。"""

import os
import time

import pytest

from server.app.video_storage import (
    LocalVideoStorage,
    UploadAbortedError,
    UploadSizeExceededError,
    VideoStorageError,
)


@pytest.fixture
def storage(tmp_path) -> LocalVideoStorage:
    """小上限 + 小复查间隔,便于触发超限/中止分支。"""
    return LocalVideoStorage(
        data_dir=tmp_path / "data",
        max_upload_bytes=1024,
        abort_check_interval_bytes=8,
    )


async def _feed(*chunks: bytes):
    for c in chunks:
        yield c


# ---------- 目录 ----------

def test_ensure_dirs(storage: LocalVideoStorage, tmp_path) -> None:
    """ensure_dirs 创建 videos/ 与 tmp/ 两级目录。"""
    videos_dir = storage.video_path("x").parent
    tmp_dir = storage.tmp_path("x").parent
    assert not videos_dir.exists()
    storage.ensure_dirs()
    assert videos_dir.is_dir()
    assert tmp_dir.is_dir()


# ---------- 上传写盘 ----------

async def test_write_upload_and_promote(storage: LocalVideoStorage) -> None:
    """流式写入 → 返回字节数 → 原子提升为正式文件;tmp 消失。"""
    n = await storage.write_upload("up1", _feed(b"hello ", b"world"))
    assert n == 11
    assert storage.tmp_path("up1").is_file()  # 写入完成仍在 tmp,等待提升

    dest = await storage.promote_upload("up1", "vid1")
    assert dest == storage.video_path("vid1")
    assert dest.read_bytes() == b"hello world"
    assert not storage.tmp_path("up1").exists()
    assert storage.stat_size("vid1") == 11


async def test_write_upload_size_exceeded(storage: LocalVideoStorage) -> None:
    """累计超限抛 UploadSizeExceededError,且临时文件被清理(验收 11)。"""
    with pytest.raises(UploadSizeExceededError):
        await storage.write_upload("up2", _feed(b"x" * 2048))
    assert not storage.tmp_path("up2").exists()


async def test_write_upload_abort_check(storage: LocalVideoStorage) -> None:
    """abort_check 返回 True 时中止(design.md 4.7 步骤 6),临时文件清理。"""
    calls: list[int] = []

    def check() -> bool:
        calls.append(1)
        return True

    with pytest.raises(UploadAbortedError):
        await storage.write_upload("up3", _feed(b"a" * 10, b"b" * 10), abort_check=check)
    assert calls  # 复查被触发(首个 chunk 达到 8 字节间隔)
    assert not storage.tmp_path("up3").exists()


async def test_write_upload_abort_check_not_triggered(storage: LocalVideoStorage) -> None:
    """abort_check 返回 False 时继续写入,不受影响。"""
    n = await storage.write_upload(
        "up4", _feed(b"a" * 10, b"b" * 10), abort_check=lambda: False
    )
    assert n == 20
    assert storage.tmp_path("up4").is_file()


async def test_abort_upload_idempotent(storage: LocalVideoStorage) -> None:
    """abort_upload 幂等:文件不存在也成功。"""
    await storage.abort_upload("nope")  # 不抛异常
    await storage.write_upload("up5", _feed(b"data"))
    await storage.abort_upload("up5")
    assert not storage.tmp_path("up5").exists()
    await storage.abort_upload("up5")  # 再次清理幂等


async def test_promote_missing_tmp_raises(storage: LocalVideoStorage) -> None:
    """提升不存在的临时文件抛 VideoStorageError。"""
    with pytest.raises(VideoStorageError):
        await storage.promote_upload("nope", "vid")


async def test_write_upload_generator_error_cleans_tmp(storage: LocalVideoStorage) -> None:
    """上传流中途异常:临时文件清理,异常向上传播。"""

    async def broken():
        yield b"ok"
        raise RuntimeError("network broken")

    with pytest.raises(RuntimeError):
        await storage.write_upload("up6", broken())
    assert not storage.tmp_path("up6").exists()


# ---------- Range 读取 ----------

async def test_open_range_basic(storage: LocalVideoStorage) -> None:
    """按 [start, start+length) 读取(验收 2 的存储层基础)。"""
    await storage.write_upload("up7", _feed(b"0123456789"))
    await storage.promote_upload("up7", "vid7")

    data = b"".join([chunk async for chunk in storage.open_range("vid7", 2, 3)])
    assert data == b"234"


async def test_open_range_beyond_eof(storage: LocalVideoStorage) -> None:
    """length 超出文件尾:返回实际可读部分。"""
    await storage.write_upload("up8", _feed(b"0123456789"))
    await storage.promote_upload("up8", "vid8")

    data = b"".join([chunk async for chunk in storage.open_range("vid8", 8, 100)])
    assert data == b"89"


async def test_open_range_from_zero(storage: LocalVideoStorage) -> None:
    """start=0 全量读取(无 Range 请求复用同一路径)。"""
    await storage.write_upload("up9", _feed(b"abcdef"))
    await storage.promote_upload("up9", "vid9")

    data = b"".join([chunk async for chunk in storage.open_range("vid9", 0, 6)])
    assert data == b"abcdef"


# ---------- 删除与清理 ----------

async def test_delete_idempotent(storage: LocalVideoStorage) -> None:
    """删除正式文件幂等(替换/清理场景,需求 D4)。"""
    await storage.write_upload("up10", _feed(b"old"))
    await storage.promote_upload("up10", "vid10")
    await storage.delete("vid10")
    assert storage.stat_size("vid10") is None
    await storage.delete("vid10")  # 幂等


async def test_sweep_tmp_removes_expired(storage: LocalVideoStorage) -> None:
    """超龄临时文件被清理并计数;未超龄保留(design.md 4.9 第 2 条)。"""
    await storage.write_upload("old1", _feed(b"old"))
    expired_ts = time.time() - 7200
    os.utime(storage.tmp_path("old1"), (expired_ts, expired_ts))

    await storage.write_upload("new1", _feed(b"new"))

    removed = await storage.sweep_tmp(max_age_seconds=3600)
    assert removed == 1
    assert not storage.tmp_path("old1").exists()
    assert storage.tmp_path("new1").is_file()


async def test_sweep_tmp_empty_dir(storage: LocalVideoStorage) -> None:
    """tmp 目录不存在时扫描返回 0(ensure_dirs 之前的兜底路径)。"""
    assert await storage.sweep_tmp(3600) == 0
