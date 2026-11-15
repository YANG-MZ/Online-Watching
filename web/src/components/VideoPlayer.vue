<template>
  <div class="player-box">
    <video ref="videoEl" :src="src" playsinline preload="metadata"></video>
    <!-- autoplay 拦截浮层(需求 7.3):点击画面恢复播放 -->
    <div v-if="autoplayBlocked" class="autoplay-overlay" @click="$emit('resume')">
      <p style="margin: 0; font-size: 18px">浏览器已拦截自动播放</p>
      <button>点击画面开始播放</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'

const props = defineProps<{
  src: string
  volume: number
  muted: boolean
  autoplayBlocked: boolean
}>()

const emit = defineEmits<{
  (e: 'video-el', el: HTMLVideoElement): void
  (e: 'resume'): void
}>()

const videoEl = ref<HTMLVideoElement | null>(null)

onMounted(() => {
  const el = videoEl.value
  if (el) {
    el.volume = props.volume
    el.muted = props.muted
    emit('video-el', el)
  }
})

// 个人音量/静音直接作用于 video 元素,与同步引擎完全隔离(需求 3.5)
watch(
  () => props.volume,
  (v) => {
    if (videoEl.value) videoEl.value.volume = v
  },
)
watch(
  () => props.muted,
  (m) => {
    if (videoEl.value) videoEl.value.muted = m
  },
)
</script>
