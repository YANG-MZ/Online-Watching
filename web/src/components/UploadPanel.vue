<template>
  <div class="card">
    <h2>上传视频(仅房主)</h2>
    <p class="muted-text">仅支持 MP4(H.264/AAC),上限 4 GB;重新上传将替换当前视频并归零进度。</p>
    <div class="controls">
      <input type="file" accept=".mp4" :disabled="uploadStore.status === 'uploading'" @change="onPick" />
      <button :disabled="!file || uploadStore.status === 'uploading'" @click="onUpload">
        {{ uploadStore.status === 'uploading' ? `上传中 ${uploadStore.progress}%` : '上传' }}
      </button>
    </div>
    <div v-if="uploadStore.status === 'uploading'" class="progress-bar">
      <div :style="{ width: uploadStore.progress + '%' }"></div>
    </div>
    <p v-if="uploadStore.status === 'done'" class="muted-text">上传完成,成员播放器已加载。</p>
    <p v-if="uploadStore.error" class="error-text">{{ uploadStore.error }}</p>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { ApiError, http } from '../api/http'
import { useSessionStore } from '../stores/session'
import { useUploadStore } from '../stores/upload'

const props = defineProps<{ roomId: string }>()

const session = useSessionStore()
const uploadStore = useUploadStore()
const file = ref<File | null>(null)

function onPick(ev: Event): void {
  const input = ev.target as HTMLInputElement
  file.value = input.files?.[0] ?? null
}

async function onUpload(): Promise<void> {
  if (!file.value) return
  uploadStore.begin(file.value.name)
  try {
    await http.uploadVideo(props.roomId, session.sessionToken, file.value, (loaded, total) =>
      uploadStore.setProgress(loaded, total),
    )
    uploadStore.done()
  } catch (e) {
    uploadStore.fail(e instanceof ApiError ? e.message : '网络错误,上传中断')
  }
}
</script>
