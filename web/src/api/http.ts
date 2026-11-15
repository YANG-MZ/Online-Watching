/**
 * HTTP 客户端(api.md 第 2 节)。
 *
 * - Base:分离部署构建时经 VITE_API_BASE_URL 注入;开发环境空串走 Vite proxy;
 * - 错误统一 {code, message}(api.md 1.1),message 为可展示中文文案;
 * - 上传走 XHR 以获得进度(design.md T8)。
 */

const BASE = (import.meta.env.VITE_API_BASE_URL ?? '').toString().replace(/\/+$/, '')

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

export interface Credentials {
  room_id: string
  user_id: string
  session_token: string
  is_host: boolean
}

export interface UploadResult {
  video_id: string
  original_name: string
  size: number
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, init)
  if (!resp.ok) {
    let code = 'UNKNOWN'
    let message = `请求失败(HTTP ${resp.status})`
    try {
      const body = (await resp.json()) as { code?: string; message?: string }
      code = body.code ?? code
      message = body.message ?? message
    } catch {
      /* 非 JSON 响应,使用默认文案 */
    }
    throw new ApiError(resp.status, code, message)
  }
  return (await resp.json()) as T
}

function authHeaders(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}` }
}

export const http = {
  /** 创建房间(api.md 2.1);invite_url 为相对路径时由前端补全。 */
  createRoom(nickname: string): Promise<Credentials> {
    return request('/api/rooms', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ nickname }),
    })
  },

  /** 加入房间(api.md 2.2);满员在 WS auth 时以 4002 拒绝。 */
  joinRoom(roomId: string, nickname: string): Promise<Credentials> {
    return request(`/api/rooms/${encodeURIComponent(roomId)}/join`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ nickname }),
    })
  },

  /** 主动退出(api.md 2.3);触发房主移交与空房倒计时由服务端完成。 */
  async leave(roomId: string, token: string): Promise<void> {
    await request(`/api/rooms/${encodeURIComponent(roomId)}/leave`, {
      method: 'POST',
      headers: authHeaders(token),
    })
  },

  /** 解散房间,仅房主(api.md 2.4)。 */
  async dissolve(roomId: string, token: string): Promise<void> {
    await request(`/api/rooms/${encodeURIComponent(roomId)}/dissolve`, {
      method: 'POST',
      headers: authHeaders(token),
    })
  },

  /** 视频文件地址(前端展示用;UUID 即访问控制,需求 4.3)。 */
  mediaUrl(videoId: string): string {
    return `${BASE}/media/${encodeURIComponent(videoId)}.mp4`
  },

  /** 邀请链接:后端返回相对路径时以当前站点补全。 */
  inviteUrl(rawInvite: string, roomId: string): string {
    if (/^https?:\/\//i.test(rawInvite)) return rawInvite
    return `${location.origin}/rooms/${encodeURIComponent(roomId)}`
  },

  /** 上传/替换视频(api.md 2.5):XHR 原始字节流,progress 回调(0~1)。 */
  uploadVideo(
    roomId: string,
    token: string,
    file: File,
    onProgress: (loaded: number, total: number) => void,
  ): Promise<UploadResult> {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest()
      xhr.open('PUT', `${BASE}/api/rooms/${encodeURIComponent(roomId)}/video`)
      xhr.setRequestHeader('Authorization', `Bearer ${token}`)
      xhr.setRequestHeader('X-Filename', encodeURIComponent(file.name))
      xhr.setRequestHeader('Content-Type', 'application/octet-stream')
      xhr.upload.onprogress = (ev) => {
        if (ev.lengthComputable) onProgress(ev.loaded, ev.total)
      }
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            resolve(JSON.parse(xhr.responseText) as UploadResult)
          } catch {
            reject(new ApiError(xhr.status, 'UNKNOWN', '上传响应解析失败'))
          }
        } else {
          let code = 'UNKNOWN'
          let message = `上传失败(HTTP ${xhr.status})`
          try {
            const body = JSON.parse(xhr.responseText) as { code?: string; message?: string }
            code = body.code ?? code
            message = body.message ?? message
          } catch {
            /* 保留默认文案 */
          }
          reject(new ApiError(xhr.status, code, message))
        }
      }
      xhr.onerror = () => reject(new ApiError(0, 'NETWORK_ERROR', '网络错误,上传中断'))
      xhr.onabort = () => reject(new ApiError(0, 'ABORTED', '上传已取消'))
      xhr.send(file)
    })
  },
}
