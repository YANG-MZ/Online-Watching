/** SyncEngine 单测(sync-protocol.md §4:事件应用/校准三档/ended/autoplay)。 */

import { describe, expect, it } from 'vitest'
import {
  DEAD_ZONE,
  RATE_ADJ_MAX,
  SMOOTH_MAX,
  SyncEngine,
  type PlaybackState,
  type VideoLike,
} from './engine'

class FakeClock {
  constructor(private now: number) {}
  serverNow(): number {
    return this.now
  }
}

class FakeVideo implements VideoLike {
  currentTime = 0
  playbackRate = 1
  duration = NaN
  paused = true
  ended = false
  playCalls = 0
  pauseCalls = 0
  play(): Promise<void> {
    this.playCalls += 1
    this.paused = false
    return Promise.resolve()
  }
  pause(): void {
    this.pauseCalls += 1
    this.paused = true
  }
}

function playingState(position: number, now: number, version = 1, rate = 1): PlaybackState {
  return { state: 'playing', position, rate, updated_at: now, version }
}

describe('version 过滤(需求 3.4.1)', () => {
  it('旧版本/同版本被丢弃', () => {
    const v = new FakeVideo()
    const engine = new SyncEngine(new FakeClock(100), v)
    const st = playingState(10, 100)
    expect(engine.applyState({ ...st, version: 5 })).toBe(true)
    expect(engine.applyState({ ...st, version: 5 })).toBe(false)
    expect(engine.applyState({ ...st, version: 3 })).toBe(false)
  })

  it('快照无条件覆盖', () => {
    const v = new FakeVideo()
    const engine = new SyncEngine(new FakeClock(100), v)
    engine.applyState(playingState(10, 100, 9))
    engine.applySnapshot(playingState(50, 100, 1)) // 版本更低也覆盖
    expect(engine.local?.version).toBe(1)
    expect(engine.local?.position).toBe(50)
  })
})

describe('校准三档(需求 3.4.3)', () => {
  it('死区内(≤0.3s):速率保持全局值', () => {
    const v = new FakeVideo()
    v.duration = 1000
    const engine = new SyncEngine(new FakeClock(110), v)
    // target = 100 + 10×1 = 110
    engine.applySnapshot(playingState(100, 100))
    v.currentTime = 110 + DEAD_ZONE // 恰在死区边界
    engine.converge()
    expect(v.playbackRate).toBe(1)
    expect(v.currentTime).toBe(110 + DEAD_ZONE)
  })

  it('渐进区间:速率微调 ±5% 内,不 seek', () => {
    const v = new FakeVideo()
    v.duration = 1000
    const engine = new SyncEngine(new FakeClock(110), v)
    engine.applySnapshot(playingState(100, 100))
    v.currentTime = 111.5 // 超前 1.5s,diff/4=0.375 超限 → 钳制到 ±5%
    engine.converge()
    expect(v.playbackRate).toBeCloseTo(1 - RATE_ADJ_MAX) // 0.95
    expect(v.currentTime).toBe(111.5) // 未硬跳
  })

  it('渐进区间:速率调整被钳制在 ±5%', () => {
    const v = new FakeVideo()
    v.duration = 1000
    const engine = new SyncEngine(new FakeClock(110), v)
    engine.applySnapshot(playingState(100, 100))
    v.currentTime = 111.5 // 超前 1.5s,diff/4=0.375 > 0.05 → 钳制
    engine.converge()
    expect(v.playbackRate).toBeCloseTo(1 - RATE_ADJ_MAX)
  })

  it('硬校准(>2s):直接 seek 且速率恢复', () => {
    const v = new FakeVideo()
    v.duration = 1000
    const engine = new SyncEngine(new FakeClock(110), v)
    engine.applySnapshot(playingState(100, 100))
    v.currentTime = 115 // 超前 5s > SMOOTH_MAX
    engine.converge()
    expect(v.currentTime).toBe(110)
    expect(v.playbackRate).toBe(1)
  })

  it('SMOOTH_MAX 常量符合协议(0.3/2.0/±5%/5s)', () => {
    expect(DEAD_ZONE).toBe(0.3)
    expect(SMOOTH_MAX).toBe(2.0)
    expect(RATE_ADJ_MAX).toBe(0.05)
  })
})

describe('暂停与播放应用', () => {
  it('paused:停止并定位到 position', () => {
    const v = new FakeVideo()
    v.paused = false
    const engine = new SyncEngine(new FakeClock(100), v)
    engine.applySnapshot({ state: 'paused', position: 42.5, rate: 1, updated_at: 90, version: 1 })
    expect(v.pauseCalls).toBe(1)
    expect(v.currentTime).toBe(42.5)
  })

  it('playing:发起播放', async () => {
    const v = new FakeVideo()
    v.paused = true
    const engine = new SyncEngine(new FakeClock(100), v)
    engine.applySnapshot(playingState(0, 100))
    await Promise.resolve() // ensurePlay 异步
    expect(v.playCalls).toBe(1)
  })
})

describe('ended 边界(sync-protocol §5)', () => {
  it('ended 时校准器暂停,不打断自然结束状态', () => {
    const v = new FakeVideo()
    v.duration = 100
    v.currentTime = 100
    v.ended = true
    const engine = new SyncEngine(new FakeClock(110), v)
    engine.markEnded() // 组件在 'ended' 事件中标记(§5:校准器暂停)
    engine.local = playingState(90, 100) // 服务端仍在推算(无全局事件)
    engine.converge()
    expect(v.currentTime).toBe(100) // 未被打断
  })

  it('ended 后房主 play:从头播放', () => {
    const v = new FakeVideo()
    v.duration = 100
    v.ended = true
    const engine = new SyncEngine(new FakeClock(110), v)
    engine.markEnded()
    engine.applySnapshot(playingState(99, 100, 2)) // target = 109 ≥ duration
    expect(v.currentTime).toBe(0) // 回到开头
    expect(engine.ended).toBe(false)
  })
})

describe('autoplay 拦截(需求 7.3)', () => {
  it('play() 被拒时置 blocked 并回调', async () => {
    let blocked = 0
    const v = new FakeVideo()
    v.paused = true
    v.play = () => {
      v.paused = true
      return Promise.reject(new DOMException('blocked', 'NotAllowedError'))
    }
    const engine = new SyncEngine(new FakeClock(100), v, () => (blocked += 1))
    engine.applySnapshot(playingState(0, 100)) // 触发一次 play(被拒)
    await Promise.resolve()
    expect(engine.autoplayBlocked).toBe(true)
    expect(blocked).toBe(1)
  })

  it('用户手势后恢复播放(第一次被拒、手势后成功)', async () => {
    let calls = 0
    const v = new FakeVideo()
    v.paused = true
    v.play = () => {
      calls += 1
      if (calls === 1) {
        v.paused = true
        return Promise.reject(new DOMException('blocked', 'NotAllowedError'))
      }
      v.paused = false
      return Promise.resolve()
    }
    const engine = new SyncEngine(new FakeClock(100), v)
    engine.applySnapshot(playingState(0, 100)) // 第一次 play 被拒
    await Promise.resolve()
    expect(engine.autoplayBlocked).toBe(true)

    engine.resumeAfterGesture() // 用户手势恢复 → 第二次 play 成功
    expect(engine.autoplayBlocked).toBe(false)
    expect(calls).toBe(2)
  })
})
