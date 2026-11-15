<template>
  <div class="page">
    <!-- 终态/错误通知浮层(api.md 3.5:4002~4005 停止重连展示终态) -->
    <div v-if="roomStore.notice" class="notice-overlay">
      <div class="notice-box">
        <h2>{{ roomStore.notice.kind === 'terminal' ? '无法继续观看' : '提示' }}</h2>
        <p>{{ roomStore.notice.message }}</p>
        <button @click="backHome">返回首页</button>
      </div>
    </div>

    <div class="room-header card">
      <h1>
        房间 {{ roomId }}
        <span class="conn-badge" :class="`conn-${roomStore.connection}`">{{ connText }}</span>
      </h1>
      <div class="controls">
        <button class="ghost" @click="copyInvite">复制邀请链接</button>
        <span v-if="copied" class="muted-text">已复制</span>
        <span class="flex-spacer"></span>
        <button v-if="isHost" class="danger" @click="onDissolve">解散房间</button>
        <button class="ghost" @click="onLeave">退出房间</button>
      </div>
      <p class="muted-text">
        邀请链接:<code>{{ inviteLink }}</code>
      </p>
    </div>

    <div class="room-layout">
      <div>
        <VideoPlayer
          v-if="roomStore.video !== null"
          :src="mediaUrl"
          :volume="preference.volume"
          :muted="preference.muted"
          :autoplay-blocked="autoplayBlocked"
          @video-el="onVideoEl"
          @resume="onResumeGesture"
        />
        <div v-else class="player-box">
          <p class="muted-text">等待房主上传视频…</p>
        </div>

        <!-- 控制条:房主可操作,成员只读(design.md 6.1;需求 3.4.2) -->
        <div v-if="roomStore.video !== null" class="card controls">
          <template v-if="isHost">
            <button @click="socket?.sendControl('pause')">暂停</button>
            <button @click="socket?.sendControl('play')">播放</button>
            <input
              v-model.number="scrub"
              type="range"
              min="0"
              :max="duration || 0"
              step="0.5"
              @change="onSeekEnd"
            />
            <select v-model="rateChoice" @change="onRateChange">
              <option value="0.5">0.5x</option>
              <option value="0.75">0.75x</option>
              <option value="1">1x</option>
              <option value="1.25">1.25x</option>
              <option value="1.5">1.5x</option>
              <option value="2">2x</option>
            </select>
          </template>
          <template v-else>
            <span class="muted-text">
              播放控制由房主操作 · 当前 {{ roomStore.playback?.state === 'playing' ? '播放中' : '已暂停' }}
            </span>
            <span class="muted-text">进度 {{ formatTime(localTime) }} / {{ formatTime(duration) }}</span>
          </template>
        </div>

        <!-- 个人音量/静音(需求 3.5:完全独立,不参与同步) -->
        <div v-if="roomStore.video !== null" class="card controls">
          <button class="ghost" @click="preference.toggleMute">
            {{ preference.muted ? '取消静音' : '静音' }}
          </button>
          <input
            type="range"
            min="0"
            max="1"
            step="0.05"
            :value="preference.volume"
            @input="onVolumeInput"
          />
          <span class="muted-text">音量 {{ Math.round(preference.volume * 100) }}%</span>
        </div>

        <UploadPanel v-if="isHost" :room-id="roomId" />
      </div>

      <MemberList :members="roomStore.members" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ApiError, http } from '../api/http'
import { RoomSocket } from '../api/ws'
import { SyncEngine, type VideoLike } from '../sync/engine'
import { usePreferenceStore } from '../stores/preference'
import { useRoomStore } from '../stores/room'
import { useSessionStore } from '../stores/session'
import { useUploadStore } from '../stores/upload'
import MemberList from '../components/MemberList.vue'
import UploadPanel from '../components/UploadPanel.vue'
import VideoPlayer from '../components/VideoPlayer.vue'

const props = defineProps<{ roomId: string }>()

const router = useRouter()
const session = useSessionStore()
const roomStore = useRoomStore()
const preference = usePreferenceStore()
const uploadStore = useUploadStore()

const scrub = ref(0)
const localTime = ref(0)
const duration = ref(0)
const rateChoice = ref('1')
const copied = ref(false)
const autoplayBlocked = ref(false)

let engine: SyncEngine | null = null
let socket: RoomSocket | null = null

const mediaUrl = computed(() => (roomStore.video ? http.mediaUrl(roomStore.video.video_id) : ''))
const inviteLink = computed(() => `${location.origin}/rooms/${props.roomId}`)
/** 房主判定(需求 3.2.2):host_user_id === 当前用户;跨 store 判定在组件层做 */
const isHost = computed(() => session.userId !== '' && roomStore.hostUserId === session.userId)
const connText = computed(
  () =>
    ({
      connecting: '连接中…',
      online: '在线',
      reconnecting: '重连中…',
      closed: '已断开',
    })[roomStore.connection],
)

function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds)) return '--:--'
  const s = Math.floor(seconds)
  const mm = String(Math.floor(s / 60)).padStart(2, '0')
  const ss = String(s % 60).padStart(2, '0')
  const hh = Math.floor(s / 3600)
  return hh > 0 ? `${hh}:${mm}:${ss}` : `${mm}:${ss}`
}

function onVideoEl(el: HTMLVideoElement): void {
  // 创建同步引擎并绑定 video(sync-protocol §4);房主与成员共用同一逻辑
  engine = new SyncEngine(socket!.clock, el as VideoLike, () => {
    autoplayBlocked.value = true
  })
  bindVideoEvents(el)
  if (roomStore.playback) engine.applySnapshot(roomStore.playback)
}

function onResumeGesture(): void {
  autoplayBlocked.value = false
  engine?.resumeAfterGesture()
}

function bindVideoEvents(el: HTMLVideoElement): void {
  el.addEventListener('loadedmetadata', () => {
    duration.value = el.duration
    if (roomStore.playback) engine?.applySnapshot(roomStore.playback)
  })
  el.addEventListener('timeupdate', () => {
    localTime.value = el.currentTime
    scrub.value = el.currentTime
  })
  el.addEventListener('ended', () => engine?.markEnded()) // 自然播完不上报服务端(需求 3.4.4)
  el.addEventListener('playing', () => engine?.unmarkEnded())
}

function onSeekEnd(): void {
  if (scrub.value >= 0) socket?.sendControl('seek', { position: scrub.value })
}

function onRateChange(): void {
  socket?.sendControl('rate', { rate: Number(rateChoice.value) })
}

function onVolumeInput(ev: Event): void {
  preference.setVolume(Number((ev.target as HTMLInputElement).value))
}

async function copyInvite(): Promise<void> {
  try {
    await navigator.clipboard.writeText(inviteLink.value)
    copied.value = true
    setTimeout(() => (copied.value = false), 2000)
  } catch {
    /* 剪贴板不可用:链接文本已展示,可手动复制 */
  }
}

async function onLeave(): Promise<void> {
  try {
    await http.leave(props.roomId, session.sessionToken)
  } catch {
    // 网络错误也继续本地退出(服务端心跳超时会兜底移除)
  }
  socket?.close()
  session.clear()
  await router.push('/')
}

async function onDissolve(): Promise<void> {
  if (!confirm('确定解散房间?全员将被移出,视频立即删除。')) return
  try {
    await http.dissolve(props.roomId, session.sessionToken)
    // room_dissolved 广播触发浮层;主动解散者直接回到首页
    socket?.close()
    session.clear()
    await router.push('/')
  } catch (e) {
    roomStore.setNotice({
      kind: 'error',
      code: e instanceof ApiError ? e.code : 'UNKNOWN',
      message: e instanceof ApiError ? e.message : '网络错误,请稍后重试',
    })
  }
}

function backHome(): void {
  socket?.close()
  session.clear()
  roomStore.reset()
  uploadStore.reset()
  void router.push('/')
}

onMounted(() => {
  // 会话不匹配路由(首次进入/清缓存):回首页引导加入(需求 3.2.1)
  if (!session.sessionToken || session.roomId !== props.roomId) {
    void router.replace({ path: '/', query: { room: props.roomId } })
    return
  }
  roomStore.reset()
  socket = new RoomSocket(props.roomId, session.sessionToken, {
    onSnapshot: (msg) => {
      roomStore.setNotice(null)
      roomStore.applySnapshot(msg)
      if (roomStore.playback) engine?.applySnapshot(roomStore.playback)
    },
    onStateUpdate: (msg) => {
      if (roomStore.applyState(msg) && roomStore.playback) {
        engine?.applyState(roomStore.playback)
      }
    },
    onEvent: (msg) => {
      switch (msg.type) {
        case 'member_joined':
          roomStore.applyMemberJoined(msg)
          break
        case 'member_left':
          roomStore.applyMemberLeft(msg)
          break
        case 'host_changed':
          roomStore.applyHostChanged(msg)
          break
        case 'video_ready':
        case 'video_changed':
          roomStore.applyVideo(msg)
          // video 对象变化 → src computed 更新自动重载;loadedmetadata 后重新对齐
          break
        default:
          break
      }
    },
    onNotice: (notice) => {
      roomStore.setNotice(notice)
      if (notice.kind === 'terminal') socket?.close()
    },
    onConnectionState: (state) => roomStore.setConnection(state),
  })
  socket.connect()
})

onBeforeUnmount(() => {
  engine?.detach()
  socket?.close()
})
</script>

<style scoped>
.flex-spacer {
  flex: 1;
}
code {
  color: #9fb4c7;
}
</style>
