/**
 * 会话 store(需求 3.2.1):凭证与身份,localStorage 持久化,
 * 刷新/重连后凭 token 恢复身份;清缓存即丢失(V1 已知限制)。
 */

import { defineStore } from 'pinia'

export const useSessionStore = defineStore('session', {
  state: () => ({
    roomId: '',
    userId: '',
    sessionToken: '',
    nickname: '',
    isHost: false,
  }),
  actions: {
    setSession(roomId: string, userId: string, sessionToken: string, nickname: string, isHost: boolean) {
      this.roomId = roomId
      this.userId = userId
      this.sessionToken = sessionToken
      this.nickname = nickname
      this.isHost = isHost
    },
    setHost(isHost: boolean) {
      this.isHost = isHost // 房主标记随移交/快照更新(需求 3.2.1:不固化在 token)
    },
    clear() {
      // 仅清除凭证;昵称保留,减少再次加入时的重复输入
      this.roomId = ''
      this.userId = ''
      this.sessionToken = ''
      this.isHost = false
    },
  },
  persist: true,
})
