<template>
  <div class="page">
    <div class="card">
      <h1>movie-togher · 多人同步观影</h1>
      <p class="muted-text">
        创建房间并分享邀请链接,由房主上传视频、控制播放;全员实时同步观看,音量/静音各自独立。
      </p>
      <h2>昵称</h2>
      <input v-model="nickname" type="text" maxlength="32" placeholder="输入昵称(1~32 字符)" />

      <h2>创建房间</h2>
      <div class="controls">
        <input
          v-model="createPassword"
          type="text"
          maxlength="64"
          placeholder="房间密码(可选,设置后加入需输入)"
        />
        <button :disabled="busy || nickname.trim() === ''" @click="onCreate">创建房间</button>
      </div>
      <p class="muted-text">创建后获得邀请链接,分享给其他人加入;若设置了密码,请一并告知。</p>

      <h2>加入房间</h2>
      <div class="controls">
        <input v-model="roomIdInput" type="text" placeholder="房间 ID 或邀请链接中的房间 ID" />
        <input v-model="joinPassword" type="text" maxlength="64" placeholder="房间密码(无密码房间留空)" />
        <button :disabled="busy || roomIdInput.trim() === '' || nickname.trim() === ''" @click="onJoin">加入</button>
      </div>
      <p v-if="errorMsg" class="error-text">{{ errorMsg }}</p>
      <p v-if="session.roomId" class="muted-text">
        上次房间:{{ session.roomId }}(<a href="#" @click.prevent="goBack">返回房间</a>;若已过期将提示重新创建)
      </p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError, http } from '../api/http'
import { useSessionStore } from '../stores/session'

const router = useRouter()
const route = useRoute()
const session = useSessionStore()

const nickname = ref(session.nickname)
const createPassword = ref('')
const roomIdInput = ref(String(route.query.room ?? ''))
const joinPassword = ref('')
const busy = ref(false)
const errorMsg = ref('')

function extractRoomId(raw: string): string {
  // 支持直接粘贴邀请链接:{origin}/rooms/{roomId}(design.md 4.10)
  const trimmed = raw.trim()
  const match = trimmed.match(/\/rooms\/([A-Za-z0-9]+)/)
  return match ? match[1] : trimmed
}

function goBack(): void {
  if (session.roomId) router.push(`/rooms/${session.roomId}`)
}

async function onCreate(): Promise<void> {
  busy.value = true
  errorMsg.value = ''
  try {
    const cred = await http.createRoom(nickname.value.trim(), createPassword.value.trim())
    session.setSession(cred.room_id, cred.user_id, cred.session_token, nickname.value.trim(), cred.is_host)
    await router.push(`/rooms/${cred.room_id}`)
  } catch (e) {
    errorMsg.value = e instanceof ApiError ? e.message : '网络错误,请稍后重试'
  } finally {
    busy.value = false
  }
}

async function onJoin(): Promise<void> {
  const roomId = extractRoomId(roomIdInput.value).toUpperCase()
  busy.value = true
  errorMsg.value = ''
  try {
    const cred = await http.joinRoom(roomId, nickname.value.trim(), joinPassword.value.trim())
    session.setSession(roomId, cred.user_id, cred.session_token, nickname.value.trim(), cred.is_host)
    await router.push(`/rooms/${roomId}`)
  } catch (e) {
    errorMsg.value = e instanceof ApiError ? e.message : '网络错误,请稍后重试'
  } finally {
    busy.value = false
  }
}
</script>
