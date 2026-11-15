/**
 * WebSocket 客户端(api.md 第 3 节,design.md 6.3)。
 *
 * - 连接后首条 auth(10s 超时,api.md 3.1);
 * - 心跳 30s ping{t0},pong 样本喂给 ClockEstimator(sync-protocol §3);
 *   重连成功后立即额外采样一次;
 * - 重连:指数退避 1s→2s→…→30s,auth_ok 成功后重置;
 * - close code 语义(api.md 3.5):4001 身份失效 / 4002 满员 /
 *   4003 房间不存在 / 4004 被踢 / 4005 解散 —— 均为终态,不重连;
 *   其余断开(网络抖动/服务重启)自动重连。
 */

import { ClockEstimator } from '../sync/clock'

const BASE = (import.meta.env.VITE_API_BASE_URL ?? '').toString().replace(/\/+$/, '')
export const AUTH_TIMEOUT_MS = 10000
const PING_INTERVAL_MS = 30000 // 需求 3.6.2

/** 指数退避(1s→2s→4s→…→30s 上限)。 */
export function reconnectDelayMs(attempt: number): number {
  return Math.min(1000 * 2 ** (attempt - 1), 30000)
}

export type ConnectionState = 'connecting' | 'online' | 'reconnecting' | 'closed'

export interface ServerMessage {
  type: string
  [key: string]: unknown
}

export interface Notice {
  kind: 'terminal' | 'error'
  code?: string
  message: string
}

export interface RoomSocketCallbacks {
  /** auth_ok 全量快照(整体覆盖本地状态,api.md 3.2)。 */
  onSnapshot(msg: ServerMessage): void
  /** 播放状态更新。 */
  onStateUpdate(msg: ServerMessage): void
  /** 其余房间事件:member_joined/left、host_changed、video_ready/changed、error 等。 */
  onEvent(msg: ServerMessage): void
  /** 终态通知(不再重连)或消息级错误提示。 */
  onNotice(notice: Notice): void
  /** 连接状态变化。 */
  onConnectionState(state: ConnectionState): void
}

function buildWsUrl(roomId: string): string {
  const path = `/ws/rooms/${encodeURIComponent(roomId)}`
  if (BASE === '') {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws'
    return `${proto}://${location.host}${path}`
  }
  const wsBase = BASE.replace(/^http/i, 'ws') // http→ws / https→wss
  return `${wsBase}${path}`
}

export class RoomSocket {
  readonly clock = new ClockEstimator()
  state: ConnectionState = 'connecting'
  private ws: WebSocket | null = null
  private attempt = 0
  private closedByUser = false
  private pingTimer: ReturnType<typeof setInterval> | null = null
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null

  constructor(
    private readonly roomId: string,
    private readonly token: string,
    private readonly cb: RoomSocketCallbacks,
  ) {}

  connect(): void {
    this.setState('connecting')
    this.open()
  }

  /** 主动关闭(离开页面/退出房间):不再重连。 */
  close(): void {
    this.closedByUser = true
    this.stopTimers()
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    this.ws?.close(1000)
    this.ws = null
    this.setState('closed')
  }

  /** 发送控制消息(房主操作,design.md 6.4:仅发送,不本地生效)。 */
  sendControl(type: 'play' | 'pause' | 'seek' | 'rate', payload: Record<string, unknown> = {}): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type, ...payload }))
    }
  }

  private setState(state: ConnectionState): void {
    this.state = state
    this.cb.onConnectionState(state)
  }

  private open(): void {
    const ws = new WebSocket(buildWsUrl(this.roomId))
    this.ws = ws
    const authTimer = setTimeout(() => {
      // 首条 auth 10s 未完成 → 断开走重连(api.md 3.1)
      if (ws.readyState === WebSocket.OPEN) ws.close(4000, 'auth timeout')
    }, AUTH_TIMEOUT_MS)
    ws.onopen = () => {
      ws.send(JSON.stringify({ type: 'auth', token: this.token }))
    }
    ws.onmessage = (ev) => {
      let msg: ServerMessage
      try {
        msg = JSON.parse(ev.data as string) as ServerMessage
      } catch {
        return // 非 JSON 忽略
      }
      clearTimeout(authTimer)
      this.handle(msg)
    }
    ws.onclose = (ev) => {
      clearTimeout(authTimer)
      this.handleClose(ev.code)
    }
    ws.onerror = () => {
      /* 错误随后触发 onclose,统一在 handleClose 处理 */
    }
  }

  private handle(msg: ServerMessage): void {
    switch (msg.type) {
      case 'auth_ok':
        this.attempt = 0
        this.setState('online')
        this.cb.onSnapshot(msg)
        this.startPing()
        this.ping() // 重连成功后立即额外采样一次(sync-protocol §3)
        break
      case 'pong': {
        // RTT/时钟样本(sync-protocol §3)
        const t0 = Number(msg.t0)
        const serverTs = Number(msg.server_ts)
        if (Number.isFinite(t0) && Number.isFinite(serverTs)) {
          const t1 = Date.now() / 1000
          const rtt = Math.max(0, t1 - t0)
          this.clock.add(rtt, serverTs + rtt / 2 - t1)
        }
        break
      }
      case 'state_update':
        this.cb.onStateUpdate(msg)
        break
      case 'room_dissolved':
        this.cb.onNotice({ kind: 'terminal', code: 'ROOM_DISSOLVED', message: '房间已解散' })
        this.cb.onEvent(msg)
        break
      case 'kicked':
        this.cb.onNotice({ kind: 'terminal', code: 'DUPLICATE_CONNECTION', message: '已在其他窗口打开本房间' })
        this.cb.onEvent(msg)
        break
      case 'error':
        this.cb.onNotice({ kind: 'error', code: String(msg.code ?? ''), message: String(msg.message ?? '操作失败') })
        break
      default:
        this.cb.onEvent(msg)
    }
  }

  private handleClose(code: number): void {
    this.stopTimers()
    this.ws = null
    if (this.closedByUser) {
      this.setState('closed')
      return
    }
    switch (code) {
      case 4001:
        this.setState('closed')
        this.cb.onNotice({ kind: 'terminal', code: 'UNAUTHORIZED', message: '身份凭证无效,请重新进入房间' })
        return
      case 4002:
        this.setState('closed')
        this.cb.onNotice({ kind: 'terminal', code: 'ROOM_FULL', message: '房间已满,无法加入' })
        return
      case 4003:
        this.setState('closed')
        this.cb.onNotice({ kind: 'terminal', code: 'ROOM_NOT_FOUND', message: '房间不存在或已关闭' })
        return
      case 4004:
        this.setState('closed')
        this.cb.onNotice({ kind: 'terminal', code: 'DUPLICATE_CONNECTION', message: '已在其他窗口打开本房间' })
        return
      case 4005:
        this.setState('closed')
        this.cb.onNotice({ kind: 'terminal', code: 'ROOM_DISSOLVED', message: '房间已解散' })
        return
      default:
        // 网络抖动/服务重启:指数退避重连(需求 3.7)
        this.attempt += 1
        this.setState('reconnecting')
        const delay = reconnectDelayMs(this.attempt)
        this.reconnectTimer = setTimeout(() => {
          if (!this.closedByUser) this.open()
        }, delay)
    }
  }

  private startPing(): void {
    this.stopPing()
    this.pingTimer = setInterval(() => this.ping(), PING_INTERVAL_MS)
  }

  private stopPing(): void {
    if (this.pingTimer !== null) {
      clearInterval(this.pingTimer)
      this.pingTimer = null
    }
  }

  private ping(): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: 'ping', t0: Date.now() / 1000 }))
    }
  }

  private stopTimers(): void {
    this.stopPing()
  }
}
