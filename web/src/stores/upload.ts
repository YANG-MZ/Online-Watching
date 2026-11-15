/**
 * 上传 store(需求 3.3):进度/状态/错误文案,供上传面板展示。
 */

import { defineStore } from 'pinia'

export type UploadStatus = 'idle' | 'uploading' | 'done' | 'error'

export const useUploadStore = defineStore('upload', {
  state: () => ({
    status: 'idle' as UploadStatus,
    progress: 0, // 0~100
    error: '',
    fileName: '',
  }),
  actions: {
    begin(fileName: string) {
      this.status = 'uploading'
      this.progress = 0
      this.error = ''
      this.fileName = fileName
    },
    setProgress(loaded: number, total: number) {
      if (total > 0) this.progress = Math.floor((loaded / total) * 100)
    },
    done() {
      this.status = 'done'
      this.progress = 100
    },
    fail(message: string) {
      this.status = 'error'
      this.error = message
    },
    reset() {
      this.status = 'idle'
      this.progress = 0
      this.error = ''
      this.fileName = ''
    },
  },
})
