/**
 * 房间 store(需求 3.1/3.4):快照驱动、整体覆盖(design.md 6.2),
 * 仅存服务端权威数据;个人偏好不进本 store(需求 3.5)。
 */

import { defineStore } from 'pinia'
import type { PlaybackState } from '../sync/engine'
import { useSessionStore } from './session'

export interface MemberInfo {
  user_id: string
  nickname: string
  is_host: boolean
}

export interface VideoInfo {
  video_id: string
  original_name: string
  size: number
}

export type ConnectionState = 'connecting' | 'online' | 'reconnecting' | 'closed'

export interface RoomNotice {
  kind: 'terminal' | 'error'
  code?: string
  message: string
}

export const useRoomStore = defineStore('room', {
  state: () => ({
    hostUserId: '',
    members: [] as MemberInfo[],
    playback: null as PlaybackState | null,
    video: null as VideoInfo | null,
    connection: 'connecting' as ConnectionState,
    notice: null as RoomNotice | null,
  }),
  getters: {
    /** 是否可播放(有视频)。 */
    hasVideo(state): boolean {
      return state.video !== null
    },
  },
  actions: {
    /** auth_ok 全量快照整体覆盖(需求 3.7,api.md 3.2)。 */
    applySnapshot(msg: Record<string, unknown>) {
      const room = msg.room as {
        host_user_id: string
        members: MemberInfo[]
        playback: PlaybackState
        video: VideoInfo | null
      }
      const you = msg.you as { user_id: string; is_host: boolean }
      this.hostUserId = room.host_user_id
      this.members = room.members
      this.playback = room.playback
      this.video = room.video
      useSessionStore().setHost(you.is_host)
    },
    /** 应用 state_update;version 过滤(需求 3.4.1),返回是否被接受。 */
    applyState(msg: Record<string, unknown>): boolean {
      const incoming = {
        state: msg.state as PlaybackState['state'],
        position: Number(msg.position),
        rate: Number(msg.rate),
        updated_at: Number(msg.updated_at),
        version: Number(msg.version),
      }
      if (this.playback !== null && incoming.version <= this.playback.version) return false
      this.playback = incoming
      return true
    },
    applyHostChanged(msg: Record<string, unknown>) {
      this.hostUserId = String(msg.host_user_id)
      // 房主标记在成员列表上同步刷新
      const session = useSessionStore()
      this.members = this.members.map((m) => ({ ...m, is_host: m.user_id === this.hostUserId }))
      session.setHost(this.hostUserId === session.userId)
    },
    applyMemberJoined(msg: Record<string, unknown>) {
      const user = msg.user as MemberInfo
      if (!this.members.some((m) => m.user_id === user.user_id)) {
        this.members.push(user)
      }
    },
    applyMemberLeft(msg: Record<string, unknown>) {
      this.members = this.members.filter((m) => m.user_id !== msg.user_id)
    },
    /** 视频就绪/变更(api.md 3.4);伴随的 state_update 由 applyState 处理。 */
    applyVideo(msg: Record<string, unknown>) {
      this.video = msg.video as VideoInfo
    },
    setNotice(notice: RoomNotice | null) {
      this.notice = notice
    },
    setConnection(state: ConnectionState) {
      this.connection = state
    },
    reset() {
      this.hostUserId = ''
      this.members = []
      this.playback = null
      this.video = null
      this.connection = 'connecting'
      this.notice = null
    },
  },
})
