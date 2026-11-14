"""PlaybackService 结算规则测试(sync-protocol.md §1/§2)。"""

import pytest

from server.app.models import PlaybackState, Room
from server.app.playback import PlaybackService


@pytest.fixture
def svc() -> PlaybackService:
    return PlaybackService()


@pytest.fixture
def room() -> Room:
    return Room(room_id="7QXK4M9R", host_user_id="u1", created_at=0.0)


def _set(pb: PlaybackState, state: str, position: float, rate: float, updated_at: float, version: int) -> None:
    pb.state = state
    pb.position = position
    pb.rate = rate
    pb.updated_at = updated_at
    pb.version = version


# ---------- compute_position(推算公式,sync-protocol §1) ----------

def test_compute_position_paused(svc: PlaybackService) -> None:
    """paused 状态:目标位置恒等于 position,与时间无关。"""
    pb = PlaybackState(state="paused", position=120.0, rate=2.0, updated_at=1000.0, version=1)
    assert svc.compute_position(pb, now=1500.0) == 120.0


def test_compute_position_playing(svc: PlaybackService) -> None:
    """playing 状态:position + elapsed × rate。"""
    pb = PlaybackState(state="playing", position=100.0, rate=2.0, updated_at=1000.0, version=1)
    assert svc.compute_position(pb, now=1010.0) == pytest.approx(120.0)


def test_compute_position_time_regression_clamped(svc: PlaybackService) -> None:
    """时间倒流(now < updated_at)时 elapsed 按 0,位置不倒退。"""
    pb = PlaybackState(state="playing", position=100.0, rate=2.0, updated_at=1000.0, version=1)
    assert svc.compute_position(pb, now=990.0) == 100.0


# ---------- apply_play ----------

def test_apply_play_from_paused(svc: PlaybackService, room: Room) -> None:
    """paused → playing:从暂停位置继续(暂停态无 elapsed 累计),基准刷新。"""
    _set(room.playback, "paused", position=100.0, rate=1.5, updated_at=1000.0, version=5)
    pb = svc.apply_play(room, now=1010.0)
    assert pb.state == "playing"
    assert pb.position == 100.0  # paused 状态推算不加 elapsed(sync-protocol §1)
    assert pb.updated_at == 1010.0
    assert pb.version == 6
    # 之后按 playing 推算:1010 → 1015 走 5s × 1.5 倍速
    assert svc.compute_position(pb, now=1015.0) == pytest.approx(107.5)


def test_apply_play_idempotent_position(svc: PlaybackService, room: Room) -> None:
    """重复 play:位置连续无跳变(幂等无害,sync-protocol §2)。"""
    _set(room.playback, "playing", position=100.0, rate=1.0, updated_at=1000.0, version=1)
    svc.apply_play(room, now=1010.0)  # position → 110, 基准 1010
    pb = svc.apply_play(room, now=1020.0)  # 结算 120 再基准化
    assert pb.position == pytest.approx(120.0)


# ---------- apply_pause ----------

def test_apply_pause_settles_position(svc: PlaybackService, room: Room) -> None:
    """playing → paused:暂停位置精确到处理时刻。"""
    _set(room.playback, "playing", position=100.0, rate=2.0, updated_at=1000.0, version=1)
    pb = svc.apply_pause(room, now=1005.0)
    assert pb.state == "paused"
    assert pb.position == pytest.approx(110.0)
    assert pb.version == 2


# ---------- apply_seek ----------

def test_apply_seek_playing_state_kept(svc: PlaybackService, room: Room) -> None:
    """playing 态 seek:state 不变,position 直接置为目标。"""
    _set(room.playback, "playing", position=100.0, rate=2.0, updated_at=1000.0, version=1)
    pb = svc.apply_seek(room, position=300.0, now=1010.0)
    assert pb.state == "playing"
    assert pb.position == 300.0
    assert pb.version == 2


def test_apply_seek_paused_exact(svc: PlaybackService, room: Room) -> None:
    """暂停态 seek 同样生效(需求 3.4.2)。"""
    _set(room.playback, "paused", position=50.0, rate=1.0, updated_at=1000.0, version=1)
    pb = svc.apply_seek(room, position=77.5, now=1001.0)
    assert pb.state == "paused"
    assert pb.position == 77.5


def test_apply_seek_negative_clamped(svc: PlaybackService, room: Room) -> None:
    """负数位置按 0 处理(sync-protocol §2:max(0, p))。"""
    _set(room.playback, "paused", position=50.0, rate=1.0, updated_at=1000.0, version=1)
    pb = svc.apply_seek(room, position=-5.0, now=1001.0)
    assert pb.position == 0.0


# ---------- apply_rate ----------

def test_apply_rate_settles_with_old_rate(svc: PlaybackService, room: Room) -> None:
    """倍速变更:先按旧倍速结算,再切换新倍速,位置连续。"""
    _set(room.playback, "playing", position=100.0, rate=2.0, updated_at=1000.0, version=1)
    pb = svc.apply_rate(room, rate=0.5, now=1010.0)
    assert pb.position == pytest.approx(120.0)  # 旧倍速 2.0 结算
    assert pb.rate == 0.5
    # 新基准下再走 10s → 120 + 10×0.5 = 125
    assert svc.compute_position(pb, now=1020.0) == pytest.approx(125.0)


# ---------- reset_for_new_video ----------

def test_reset_for_new_video(svc: PlaybackService, room: Room) -> None:
    """替换视频重置(需求 D4):paused/0/1.0,version+1 不归零。"""
    _set(room.playback, "playing", position=500.0, rate=2.0, updated_at=1000.0, version=17)
    pb = svc.reset_for_new_video(room, now=2000.0)
    assert pb.state == "paused"
    assert pb.position == 0.0
    assert pb.rate == 1.0
    assert pb.updated_at == 2000.0
    assert pb.version == 18  # 延续递增,旧视频迟到事件仍可被过滤


def test_version_monotonic_across_ops(svc: PlaybackService, room: Room) -> None:
    """任意操作序列 version 严格单调递增(需求 3.4.1)。"""
    _set(room.playback, "paused", position=0.0, rate=1.0, updated_at=0.0, version=0)
    versions = [
        svc.apply_play(room, now=1.0).version,
        svc.apply_pause(room, now=2.0).version,
        svc.apply_seek(room, position=9.0, now=3.0).version,
        svc.apply_rate(room, rate=2.0, now=4.0).version,
        svc.reset_for_new_video(room, now=5.0).version,
    ]
    assert versions == [1, 2, 3, 4, 5]
