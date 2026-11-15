/**
 * 个人播放偏好 store(需求 3.5):音量/静音完全独立、不上报服务端,
 * localStorage 持久化(刷新后保留)。全屏为元素级状态不入 store(design.md 6.2)。
 */

import { defineStore } from 'pinia'

export const usePreferenceStore = defineStore('preference', {
  state: () => ({
    volume: 0.8, // 0~1
    muted: false,
  }),
  actions: {
    setVolume(volume: number) {
      this.volume = Math.max(0, Math.min(1, volume))
      if (this.volume > 0 && this.muted) {
        this.muted = false // 调整音量视为解除静音的常见习惯
      }
    },
    toggleMute() {
      this.muted = !this.muted
    },
  },
  persist: true,
})
