"""权威播放状态结算服务(design.md 4.5 引用,sync-protocol.md §1/§2)。

本阶段仅定义接口;结算规则以 sync-protocol.md §2 为准:

| 操作    | 规则 |
| play    | position = target(now)(原为 playing 则不变),state=playing,version+1 |
| pause   | position = target(now),state=paused,version+1 |
| seek    | position = max(0, p),state 不变,version+1 |
| rate    | 先按旧倍速结算 position,再写新 rate,version+1 |
| 替换重置 | state=paused, position=0, rate=1.0, version+1(不归零) |

依赖方向:playback → models。
所有方法应在事件循环线程内同步调用(design.md 4.6,天然无锁)。
"""

from __future__ import annotations

from server.app.models import PlaybackState, Room


class PlaybackService:
    """权威播放状态的纯计算与写入门面(不负责广播,广播由 ws 层完成)。"""

    @staticmethod
    def compute_position(playback: PlaybackState, now: float) -> float:
        """按权威状态推算 now 时刻的目标位置(sync-protocol.md §1 公式)。

        target = playing ? position + (now - updated_at) × rate : position
        """

    def apply_play(self, room: Room, now: float) -> PlaybackState:
        """结算并置为 playing(幂等);返回更新后的权威状态。"""

    def apply_pause(self, room: Room, now: float) -> PlaybackState:
        """结算当前位置并置为 paused;返回更新后的权威状态。"""

    def apply_seek(self, room: Room, position: float, now: float) -> PlaybackState:
        """跳转(暂停态同样生效,需求 3.4.2);position 负数按 0 处理。"""

    def apply_rate(self, room: Room, rate: float, now: float) -> PlaybackState:
        """倍速变更:先按旧倍速结算再写新倍速(sync-protocol.md §2)。"""

    def reset_for_new_video(self, room: Room, now: float) -> PlaybackState:
        """视频上传/替换后的状态重置(需求 D4):paused/0/1.0,version+1。"""
