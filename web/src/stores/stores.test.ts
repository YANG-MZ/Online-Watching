/** Pinia store 单测:preference 持久化(需求 3.5)、session、room 事件应用。 */

import { beforeEach, describe, expect, it } from 'vitest'
import { createApp, nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import piniaPluginPersistedstate from 'pinia-plugin-persistedstate'
import { usePreferenceStore } from './preference'
import { useSessionStore } from './session'
import { useRoomStore } from './room'

function freshPinia() {
  const pinia = createPinia()
  pinia.use(piniaPluginPersistedstate)
  // pinia v3:插件在 app.use(pinia)(install)时才从暂存队列刷入;
  // 测试环境无真实应用,需以空应用完成安装激活插件。
  createApp({ render: () => null }).use(pinia)
  setActivePinia(pinia)
  return pinia
}

describe('preference store(需求 3.5:独立偏好 + 持久化)', () => {
  beforeEach(() => {
    localStorage.clear()
    freshPinia()
  })

  it('音量钳制 0~1', () => {
    const p = usePreferenceStore()
    p.setVolume(1.5)
    expect(p.volume).toBe(1)
    p.setVolume(-2)
    expect(p.volume).toBe(0)
  })

  it('调整音量解除静音', () => {
    const p = usePreferenceStore()
    p.muted = true
    p.setVolume(0.6)
    expect(p.muted).toBe(false)
    expect(p.volume).toBe(0.6)
  })

  it('静音切换', () => {
    const p = usePreferenceStore()
    p.toggleMute()
    expect(p.muted).toBe(true)
    p.toggleMute()
    expect(p.muted).toBe(false)
  })

  it('刷新后从 localStorage 恢复(persist 插件)', async () => {
    const p = usePreferenceStore()
    p.setVolume(0.35)
    p.muted = true
    await nextTick() // persist 插件经 $subscribe(flush: 'pre')异步写入

    // 模拟刷新:新 pinia 实例重新读回
    freshPinia()
    const p2 = usePreferenceStore()
    expect(p2.volume).toBeCloseTo(0.35)
    expect(p2.muted).toBe(true)
  })
})

describe('session store(需求 3.2.1)', () => {
  beforeEach(() => {
    localStorage.clear()
    freshPinia()
  })

  it('setSession/clear 与房主标记更新', () => {
    const s = useSessionStore()
    s.setSession('R1', 'u1', 'tk', 'Alice', true)
    expect(s.isHost).toBe(true)
    s.setHost(false) // 移交后(需求 3.1.5)
    expect(s.isHost).toBe(false)
    s.clear()
    expect(s.roomId).toBe('')
    expect(s.sessionToken).toBe('')
  })

  it('持久化:刷新后恢复身份凭证', async () => {
    const s = useSessionStore()
    s.setSession('ROOM01', 'u1', 'tk', 'Alice', true)
    await nextTick() // persist 异步写入
    freshPinia()
    const s2 = useSessionStore()
    expect(s2.roomId).toBe('ROOM01')
    expect(s2.sessionToken).toBe('tk')
  })
})

describe('room store(快照驱动,api.md 3.2)', () => {
  beforeEach(() => {
    localStorage.clear()
    freshPinia()
  })

  function snapshotMsg() {
    return {
      you: { user_id: 'u1', is_host: true },
      room: {
        room_id: 'R1',
        host_user_id: 'u1',
        members: [{ user_id: 'u1', nickname: 'Alice', is_host: true }],
        playback: { state: 'paused', position: 0, rate: 1, updated_at: 100, version: 3 },
        video: { video_id: 'vid', original_name: 'a.mp4', size: 10 },
      },
    }
  }

  it('applySnapshot 整体覆盖并同步 session 房主标记', () => {
    const room = useRoomStore()
    const session = useSessionStore()
    session.setSession('R1', 'u1', 'tk', 'Alice', false)
    room.applySnapshot(snapshotMsg())
    expect(room.hostUserId).toBe('u1')
    expect(session.isHost).toBe(true) // 来自快照 you.is_host
    expect(room.video !== null).toBe(true)
  })

  it('成员加入/离开/房主移交', () => {
    const room = useRoomStore()
    const session = useSessionStore()
    session.setSession('R1', 'u2', 'tk2', 'Bob', false)
    room.applySnapshot({
      you: { user_id: 'u2', is_host: false },
      room: {
        room_id: 'R1',
        host_user_id: 'u1',
        members: [{ user_id: 'u1', nickname: 'Alice', is_host: true }],
        playback: { state: 'paused', position: 0, rate: 1, updated_at: 100, version: 3 },
        video: null,
      },
    })

    room.applyMemberJoined({ user: { user_id: 'u2', nickname: 'Bob', is_host: false } })
    expect(room.members.length).toBe(2)
    room.applyMemberJoined({ user: { user_id: 'u2', nickname: 'Bob', is_host: false } }) // 去重(验收 8)
    expect(room.members.length).toBe(2)

    room.applyHostChanged({ host_user_id: 'u2' }) // 房主移交(需求 3.1.5)
    expect(room.hostUserId).toBe('u2')
    expect(session.isHost).toBe(true)

    room.applyMemberLeft({ user_id: 'u1', reason: 'leave' })
    expect(room.members.length).toBe(1)
  })

  it('applyState 更新播放状态', () => {
    const room = useRoomStore()
    room.applyState({
      state: 'playing',
      position: 300,
      rate: 1.5,
      updated_at: 200,
      version: 9,
    })
    expect(room.playback?.version).toBe(9)
    expect(room.playback?.rate).toBe(1.5)
  })
})
