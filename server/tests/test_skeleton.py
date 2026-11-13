"""骨架冒烟测试:验证配置加载、标识生成与数据结构可用(不涉及业务逻辑)。"""

from server.app.config import Settings, load_settings
from server.app.models import Member, PlaybackState, Room, VideoMeta
from server.app.security import (
    generate_room_id,
    generate_session_token,
    generate_user_id,
    generate_video_id,
)


def test_default_settings_loaded() -> None:
    """默认配置满足需求硬性上限(design.md 第 7 节)。"""
    s = Settings()
    assert s.limits.max_rooms == 5
    assert s.limits.max_members_per_room == 10
    assert s.limits.max_upload_bytes == 4 * 1024**3
    assert s.room.empty_expire_seconds == 600
    assert s.heartbeat.server_timeout_seconds == 90


def test_load_settings_missing_file_uses_defaults(tmp_path) -> None:
    """配置文件不存在时使用全默认值(config.py load_settings 契约)。"""
    s = load_settings(tmp_path / "not-exist.toml")
    assert s.server.port == 8000


def test_security_ids_format() -> None:
    """标识格式符合 design.md 4.10:房间 ID 8 位、user_id 16 位 hex、token 43 字符。"""
    room_id = generate_room_id()
    assert len(room_id) == 8
    assert all(ch in "23456789ABCDEFGHJKLMNPQRSTUVWXYZ" for ch in room_id)
    assert len(generate_user_id()) == 16
    assert len(generate_session_token()) == 43
    assert len(generate_video_id()) == 32


def test_room_model_defaults() -> None:
    """内存房间对象默认字段(design.md 4.2)。"""
    room = Room(room_id="7QXK4M9R", host_user_id="a3f8", created_at=1000.0)
    assert room.playback.state == "paused"
    assert room.playback.version == 0
    assert room.members == {}
    assert room.video is None
    assert room.empty_since is None
    assert isinstance(Member(user_id="u", nickname="n", joined_at=0.0, last_seen=0.0).conn is None, bool)
    assert isinstance(VideoMeta(video_id="v", original_name="a.mp4", size=1).size, int)
