/**
 * 客户端同步引擎(sync-protocol.md §4)。
 *
 * - applySnapshot/applyState:应用服务端权威状态(version 过滤);
 * - converge:周期校准(死区 0.3s / 渐进 0.3~2s 速率 ±5% / 硬校准 >2s);
 * - autoplay 拦截处理:play() 被拒进入待播放状态,由用户手势恢复;
 * - ended:自然播完暂停校准,再次播放从 0 开始(§5 边界)。
 */

export interface PlaybackState {
  state: 'playing' | 'paused'
  position: number
  rate: number
  updated_at: number
  version: number
}

/** 与 HTMLVideoElement 兼容的最小接口(便于测试替身)。 */
export interface VideoLike {
  currentTime: number
  playbackRate: number
  readonly duration: number
  readonly paused: boolean
  readonly ended: boolean
  play(): Promise<void>
  pause(): void
}

// 校准参数(sync-protocol.md §4.2 参数表)
export const CALIB_INTERVAL_MS = 5000
export const DEAD_ZONE = 0.3
export const SMOOTH_MAX = 2.0
export const RATE_ADJ_MAX = 0.05

function clamp(value: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, value))
}

export class SyncEngine {
  local: PlaybackState | null = null
  autoplayBlocked = false
  ended = false
  private timer: ReturnType<typeof setInterval> | null = null

  constructor(
    private clock: { serverNow(): number },
    private video: VideoLike | null = null,
    private onBlocked: (() => void) | null = null,
  ) {}

  attach(video: VideoLike, onBlocked: (() => void) | null = null): void {
    this.video = video
    if (onBlocked) this.onBlocked = onBlocked
    this.start()
    this.syncImmediate()
  }

  detach(): void {
    this.stop()
    this.video = null
  }

  start(): void {
    if (this.timer === null) {
      this.timer = setInterval(() => this.converge(), CALIB_INTERVAL_MS)
    }
  }

  stop(): void {
    if (this.timer !== null) {
      clearInterval(this.timer)
      this.timer = null
    }
  }

  /** 快照整体覆盖(无条件,api.md 3.2)。 */
  applySnapshot(pb: PlaybackState): void {
    this.local = { ...pb }
    this.syncImmediate()
  }

  /** 应用 state_update;version 过滤(需求 3.4.1),返回是否被接受。 */
  applyState(pb: PlaybackState): boolean {
    if (this.local !== null && pb.version <= this.local.version) return false
    this.local = { ...pb }
    this.syncImmediate()
    return true
  }

  /** 用户手势后恢复播放(autoplay 拦截解除)。 */
  resumeAfterGesture(): void {
    this.autoplayBlocked = false
    this.syncImmediate()
  }

  markEnded(): void {
    this.ended = true
    if (this.local && this.video) this.video.playbackRate = this.local.rate
  }

  unmarkEnded(): void {
    this.ended = false
  }

  /** 推算 now 时刻的目标位置(sync-protocol §1 公式)。 */
  computeTarget(now: number): number {
    const pb = this.local
    if (pb === null) return 0
    if (pb.state !== 'playing') return pb.position
    const elapsed = Math.max(0, now - pb.updated_at)
    return pb.position + elapsed * pb.rate
  }

  /** 收到权威状态后立即对齐(playing → play + 校准;paused → 停 + 定位)。 */
  syncImmediate(): void {
    const pb = this.local
    const video = this.video
    if (pb === null || video === null) return
    if (pb.state === 'playing') {
      // ended 后房主 play:从头播放(§5,与浏览器 ended→play 一致)
      if (
        this.ended ||
        (Number.isFinite(video.duration) && video.duration > 0 && this.computeTarget(this.clock.serverNow()) >= video.duration)
      ) {
        video.currentTime = 0
        this.unmarkEnded()
      }
      // 播放发起与周期检测统一由 converge 内的 ensurePlay 完成,避免双重调用
      this.converge()
    } else {
      video.pause()
      video.currentTime = this.clampToDuration(pb.position)
    }
  }

  /** 周期校准(需求 3.4.3);仅 playing 时执行;ended 后暂停(§5)。 */
  converge(): void {
    const pb = this.local
    const video = this.video
    if (pb === null || video === null || pb.state !== 'playing') return
    if (this.ended || video.ended) return // 校准器在 ended 后暂停运行,不打断自然结束

    let target = this.computeTarget(this.clock.serverNow())
    if (Number.isFinite(video.duration) && video.duration > 0 && target >= video.duration) {
      // 推算越过片尾:从头开始(§5,与浏览器 ended→play 全员同路径)
      target = 0
      this.unmarkEnded()
    }
    target = this.clampToDuration(target)
    const diff = video.currentTime - target // >0 本地超前

    if (Math.abs(diff) <= DEAD_ZONE) {
      video.playbackRate = pb.rate
    } else if (Math.abs(diff) <= SMOOTH_MAX) {
      // 渐进校准:超前放慢 / 落后加快,最多 ±5%(§4.2)
      video.playbackRate = pb.rate * (1 - clamp(diff / 4, -RATE_ADJ_MAX, RATE_ADJ_MAX))
    } else {
      // 硬校准:直接 seek
      video.playbackRate = pb.rate
      video.currentTime = target
    }
    void this.ensurePlay()
  }

  private clampToDuration(t: number): number {
    const video = this.video
    if (video === null) return Math.max(0, t)
    if (Number.isFinite(video.duration) && video.duration > 0) {
      return clamp(t, 0, video.duration)
    }
    return Math.max(0, t)
  }

  private async ensurePlay(): Promise<void> {
    const video = this.video
    if (video === null || !video.paused) return
    try {
      await video.play()
      this.autoplayBlocked = false
    } catch {
      // autoplay 策略拦截(需求 7.3):进入待播放状态,等待用户手势;
      // 每 5s 周期 converge 会再次检测(§4.1)。
      this.autoplayBlocked = true
      this.onBlocked?.()
    }
  }
}
