"""权威播放状态结算服务(design.md 4.5,sync-protocol.md §1/§2)。

结算规则(sync-protocol.md §2,处理时刻 now 为服务端时钟):

| 操作    | 规则 |
| play    | position = target(now)(原为 playing 则结算后不变),state=playing,version+1 |
| pause   | position = target(now),state=paused,version+1 |
| seek    | position = max(0, p),state 不变,version+1 |
| rate    | 先按旧倍速结算 position,再写新 rate,version+1 |
| 替换重置 | state=paused, position=0, rate=1.0, version+1(version 不归零) |

所有方法在事件循环线程内同步调用(design.md 4.6,天然无锁);
本服务为纯计算,不做倍速范围校验(由 ws 层按 [allowed_rate_min,
allowed_rate_max] 校验后调用),也不负责广播(由 ws 层完成)。

依赖方向:playback → models。
"""

from __future__ import annotations

from server.app.models import PlaybackState, Room


class PlaybackService:
    """权威播放状态的推算与结算(每房间一个 Room,状态在 room.playback)。"""

    @staticmethod
    def compute_position(playback: PlaybackState, now: float) -> float:
        """按权威状态推算 now 时刻的目标位置(sync-protocol.md §1 公式)。

        target = playing ? position + (now - updated_at) × rate : position
        时间倒流(now < updated_at,时钟异常)时 elapsed 按 0 处理。
        """
        elapsed = max(0.0, now - playback.updated_at)
        if playback.state == "playing":
            return playback.position + elapsed * playback.rate
        return playback.position

    def apply_play(self, room: Room, now: float) -> PlaybackState:
        """结算并置为 playing;重复 play 仅重新基准化,位置连续无害(幂等)。"""
        pb = room.playback
        pb.position = self.compute_position(pb, now)
        pb.state = "playing"
        pb.updated_at = now
        pb.version += 1
        return pb

    def apply_pause(self, room: Room, now: float) -> PlaybackState:
        """结算当前位置并置为 paused,暂停位置精确到处理时刻。"""
        pb = room.playback
        pb.position = self.compute_position(pb, now)
        pb.state = "paused"
        pb.updated_at = now
        pb.version += 1
        return pb

    def apply_seek(self, room: Room, position: float, now: float) -> PlaybackState:
        """跳转:state 不变,暂停态同样生效(需求 3.4.2);负数按 0 处理。"""
        pb = room.playback
        pb.position = max(0.0, position)
        pb.updated_at = now
        pb.version += 1
        return pb

    def apply_rate(self, room: Room, rate: float, now: float) -> PlaybackState:
        """倍速变更:先按旧倍速结算 position,再写新倍速,保证位置连续。"""
        pb = room.playback
        pb.position = self.compute_position(pb, now)
        pb.rate = rate
        pb.updated_at = now
        pb.version += 1
        return pb

    def reset_for_new_video(self, room: Room, now: float) -> PlaybackState:
        """视频上传/替换后的状态重置(需求 D4):paused/0/1.0。

        version 延续递增不归零,保证旧视频的迟到事件仍能被过滤
        (sync-protocol.md §2"视频替换重置")。
        """
        pb = room.playback
        pb.state = "paused"
        pb.position = 0.0
        pb.rate = 1.0
        pb.updated_at = now
        pb.version += 1
        return pb
