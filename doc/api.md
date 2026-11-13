# 接口与通信协议设计(HTTP API + WebSocket)

| 项目名称 | movie-togher |
| --- | --- |
| 文档版本 | A1.0 |
| 更新日期 | 2026-11-14 |
| 需求依据 | doc/requirements.md(V1.1) |
| 配套文档 | doc/design.md(总体方案)、doc/sync-protocol.md(同步协议) |

---

## 1. 通用约定

- **Base URL**:开发环境同源(Vite proxy);分离部署为后端地址,如 `http://<IP>:8000`;
- **数据格式**:HTTP 请求/响应均为 `application/json; charset=utf-8`(视频上传/下发除外);
- **认证**:创建/加入房间无需凭证;其余请求携带 `Authorization: Bearer {session_token}`;
- **时间**:除特别说明外,时间戳均为 unix 秒(浮点,服务端时钟);播放位置单位为秒(浮点);
- **CORS**:后端按 `config.toml [cors].allowed_origins` 放行(分离部署需要);WebSocket 不受 CORS 约束。

### 1.1 错误响应结构

```json
{ "code": "ROOM_FULL", "message": "房间已满(10/10)" }
```

`message` 为可直接展示的简体中文文案;前端按 `code` 映射文案,`message` 兜底。

### 1.2 HTTP 状态码使用

| HTTP 状态 | 场景 |
| --- | --- |
| 400 | 参数缺失/非法(昵称为空、扩展名非 mp4、seek 位置为负等) |
| 401 | 缺少/无效 session_token |
| 403 | token 有效但非房主(需求 3.2.2) |
| 404 | 房间不存在(含重启后失效,需求 4.5) |
| 409 | 房间内已有进行中的上传 |
| 413 | 上传超过大小上限 |
| 429 | 房间数达到上限(拒绝创建) |

---

## 2. HTTP API

### 2.1 创建房间

```
POST /api/rooms
{ "nickname": "Alice" }
```

**成功 200**:

```json
{
  "room_id": "7QXK4M9R",
  "invite_url": "http://<前端地址>/rooms/7QXK4M9R",
  "user_id": "a3f8c92e11b04d77",
  "session_token": "<token_urlsafe_43>",
  "is_host": true
}
```

**失败**:`429 {code:"ROOM_LIMIT_REACHED"}`(当前房间数已达上限);`400 {code:"INVALID_NICKNAME"}`(昵称为空或超长,1~32 字符)。

说明:创建即写 SQLite `rooms` 行;**不占用在线名额**(进入房间以 WS auth 为准,见 3.2);创建者若从未连 WS,房间按空房倒计时过期。

### 2.2 加入房间

```
POST /api/rooms/{room_id}/join
{ "nickname": "Bob" }
```

**成功 200**:

```json
{
  "room_id": "7QXK4M9R",
  "user_id": "b8d1...",
  "session_token": "<token>",
  "is_host": false
}
```

**失败**:`404 ROOM_NOT_FOUND`;`400 INVALID_NICKNAME`。

说明:满员(在线人数 ≥ 上限)不在此接口拒绝,而在 WS auth 时以 close 4002 拒绝(在线名额以 WS 连接为准,design.md 4.4);HTTP 层无法准确判定瞬时在线数,统一口径避免"HTTP 说能进、WS 进不去"以外的歧义——前端收到 4002 时展示"房间已满"。

### 2.3 主动退出房间

```
POST /api/rooms/{room_id}/leave
Authorization: Bearer {session_token}
```

**成功 200**:`{ "ok": true }`

**失败**:`401 UNAUTHORIZED`;`404 ROOM_NOT_FOUND`。

行为(需求 3.1.4/3.1.5):

1. 服务端移除该成员(在房则移除,不在房幂等成功)、关闭其 WS 连接;
2. 广播 `member_left{user_id, reason:"leave"}`;
3. **若退出者为房主**:立即移交,广播 `host_changed`;
4. 若房间变空:启动 10 分钟过期倒计时。

### 2.4 解散房间(仅房主)

```
POST /api/rooms/{room_id}/dissolve
Authorization: Bearer {session_token}
```

**成功 200**:`{ "ok": true }`

**失败**:`403 FORBIDDEN`(非房主);`404`;`401`。

行为(需求 3.1.6):向全体 WS 广播 `room_dissolved` → 关闭全部连接 → 删除视频文件与 SQLite 行 → 销毁内存房间。**立即**完成,不等清理周期。

### 2.5 上传/替换视频(仅房主)

```
PUT /api/rooms/{room_id}/video
Authorization: Bearer {session_token}
Headers:
  X-Filename: <URL编码后的原始文件名,须以 .mp4 结尾>
  Content-Type: application/octet-stream
  Content-Length: <字节数>
Body: 视频原始字节流(整体流式,不分片,需求 3.3)
```

**成功 200**:

```json
{ "video_id": "<uuid4hex>", "original_name": "movie.mp4", "size": 1234567890 }
```

**失败**(均立即返回,已写的临时文件删除):

| 状态 | code | 场景 |
| --- | --- | --- |
| 400 | INVALID_FORMAT | 扩展名非 `.mp4`(验收 11) |
| 413 | FILE_TOO_LARGE | Content-Length 超上限,或流式累计超上限(验收 11) |
| 409 | UPLOAD_IN_PROGRESS | 该房间已有进行中的上传 |
| 403 | FORBIDDEN | 非房主(含上传过程中房主被移交,周期复查命中) |
| 404 | ROOM_NOT_FOUND | 房间不存在/上传中房间被解散 |

行为:完成后权威状态重置(paused/0/1.0/version+1),广播 `video_ready`(首次)或 `video_changed`(替换)(需求 3.3/D4)。

### 2.6 视频文件访问

```
GET /media/{video_id}.mp4
可选: Range: bytes=<start>-[<end>]
可选: HEAD
```

- 支持 `Range` 单段请求:返回 `206 Partial Content` + `Content-Range: bytes start-end/total` + `Accept-Ranges: bytes`(验收 2);
- 无 `Range`:`200` 全量;起止越界:`416`;`video_id` 不存在:`404`;
- 不做逐请求鉴权(需求 4.3:UUID 即最低访问控制);
- 响应头:`Content-Type: video/mp4`、`Content-Length`;分块异步输出(1 MiB/块)。

### 2.7 健康检查

```
GET /healthz → 200 {"status":"ok"}
```

---

## 3. WebSocket 协议

### 3.1 连接与鉴权

```
ws://<后端>/ws/rooms/{room_id}
```

- 连接建立后**首条消息**必须为 `auth`(10s 内,超时 close 4001);
- 鉴权失败:回 `error` 消息后 close(见 3.5 close code 表);
- 鉴权成功:回 `auth_ok`(含全量快照)→ 加入在线成员 → 房间广播 `member_joined`;
- 断线由客户端指数退避重连,重连后同样走 `auth` + 全量快照(需求 3.7);
- 同 token 第二条连接:旧连接收到 `kicked` 后被关闭(决策 T15)。

### 3.2 全量快照结构(auth_ok 携带,需求 3.7)

```json
{
  "you": { "user_id": "a3f8...", "is_host": true },
  "room": {
    "room_id": "7QXK4M9R",
    "host_user_id": "a3f8...",
    "members": [
      { "user_id": "a3f8...", "nickname": "Alice", "is_host": true },
      { "user_id": "b8d1...", "nickname": "Bob", "is_host": false }
    ],
    "playback": {
      "state": "playing",
      "position": 500.0,
      "rate": 1.0,
      "updated_at": 1763120400.123,
      "version": 17
    },
    "video": { "video_id": "<uuid>", "original_name": "movie.mp4", "size": 123 }
  },
  "server_ts": 1763120400.456
}
```

快照语义:客户端以快照**整体覆盖**本地房间状态(不增量合并),随后仅接受 `version` 更大的 `state_update`。

### 3.3 客户端 → 服务端

所有消息统一信封:`{ "type": "...", ... }`。除 `auth`/`ping` 外,均要求已鉴权。

| type | 载荷 | 权限 | 说明 |
| --- | --- | --- | --- |
| `auth` | `{ "token": "..." }` | — | 连接后首条,10s 超时 |
| `ping` | `{ "t0": 1763120370.1 }` | 成员 | 30s 周期;刷新 last_seen;用于 RTT/时钟估计 |
| `play` | `{}` | 房主 | 请求全局播放 |
| `pause` | `{}` | 房主 | 请求全局暂停 |
| `seek` | `{ "position": 300.0 }` | 房主 | 拖动结束后发送(需求 3.4.2);position ≥ 0 |
| `rate` | `{ "rate": 1.5 }` | 房主 | 范围 [0.25, 3.0],超范围回 error |

非房主发送控制消息:回 `error{code:"FORBIDDEN"}`,不断开连接(验收 9)。

### 3.4 服务端 → 客户端

| type | 载荷 | 触发 |
| --- | --- | --- |
| `auth_ok` | 快照(见 3.2) | auth 成功 |
| `pong` | `{ "t0": <回显>, "server_ts": ... }` | 响应 ping |
| `member_joined` | `{ "user": {user_id, nickname, is_host} }` | 新成员 auth 成功 |
| `member_left` | `{ "user_id": "...", "reason": "leave"/"timeout" }` | 主动退出 / 心跳超时 |
| `host_changed` | `{ "host_user_id": "..." }` | 房主移交(需求 3.1.5) |
| `state_update` | `{ "state","position","rate","updated_at","version","server_ts" }` | 任一控制操作生效后广播 |
| `video_ready` | `{ "video": {...} }` | 首次上传完成 |
| `video_changed` | `{ "video": {...} }` | 替换上传完成(播放器重新加载,验收 12) |
| `room_dissolved` | `{ "reason": "host_dissolved" }` | 解散,随 connection 关闭 |
| `kicked` | `{ "reason": "duplicate_connection" }` | 同 token 新连接接管(决策 T15) |
| `error` | `{ "code": "...", "message": "..." }` | 消息级错误(如 FORBIDDEN) |

`state_update` 字段语义与结算规则见 sync-protocol.md §2;`version` 单调递增,客户端丢弃 version ≤ 本地已知的更新。

### 3.5 WebSocket Close Code 约定

| code | 含义 | 客户端行为 |
| --- | --- | --- |
| 1000 | 服务端正常关闭 | 停止 |
| 4001 | 鉴权失败(token 无效/超时未 auth) | 回首页重新加入,提示身份失效 |
| 4002 | 房间满员(在线口径) | 展示"房间已满",停止重连 |
| 4003 | 房间不存在(含过期/重启失效) | 展示"房间不存在或已关闭",停止重连 |
| 4004 | 被新连接取代(duplicate_connection) | 提示"已在其他窗口打开",本页停用 |
| 4005 | 房间已解散 | 展示"房间已解散",停止重连 |

4002~4005 为终态,**不**触发自动重连;其余断开(网络抖动、服务重启)按指数退避重连。

---

## 4. 错误码总表

| code | HTTP/WS | 文案建议 |
| --- | --- | --- |
| INVALID_NICKNAME | 400 | 昵称不能为空,且不超过 32 个字符 |
| ROOM_NOT_FOUND | 404 / WS 4003 | 房间不存在或已关闭 |
| ROOM_LIMIT_REACHED | 429 | 房间数量已达上限,请稍后再试 |
| ROOM_FULL | WS 4002 | 房间已满,无法加入 |
| UNAUTHORIZED | 401 / WS 4001 | 身份凭证无效,请重新进入房间 |
| FORBIDDEN | 403 / error 消息 | 仅房主可执行此操作 |
| INVALID_FORMAT | 400 | 仅支持 .mp4 文件 |
| FILE_TOO_LARGE | 413 | 文件超过大小上限(4 GB) |
| UPLOAD_IN_PROGRESS | 409 | 已有上传正在进行,请稍候 |
| SEEK_OUT_OF_RANGE | error 消息 | 跳转位置非法 |
| RATE_OUT_OF_RANGE | error 消息 | 倍速超出允许范围 |
| ROOM_DISSOLVED | WS 4005 | 房间已解散 |
| DUPLICATE_CONNECTION | WS 4004 | 已在其他窗口打开本房间 |

---

## 5. 关键时序

### 5.1 创建 → 加入 → 首次上传

```
A(房主)                    服务端                       B/C(成员)
 │ POST /api/rooms          │                            │
 │ ────────────────────────▶│ 建房+DB 行,签发 token        │
 │ ◀─── room_id/token ──────│                            │
 │ WS connect + auth ──────▶│ 加入在线成员                  │
 │ ◀─── auth_ok(快照) ──────│                            │
 │                          │──── member_joined ─────────▶│(B/C 加入后)
 │ PUT /video(流式)────────▶│ tmp.part → rename → DB      │
 │ ◀─── 200 video ──────────│                            │
 │                          │──── video_ready ──────────▶│
 │                          │◀──────── WS connect ────────│
```

### 5.2 播放控制(需求 3.4.2)

```
A(房主)                     服务端                        B
 │ play ────────────────────▶│ 权威状态结算(position 推算至 now,   │
 │                          │  state=playing, version+1)      │
 │ ◀─────── state_update ────┤──────── state_update ───────▶│
 │ 本地按 sync-protocol 应用   │              本地校准到目标位置并播放 │
```

### 5.3 房主主动退出与移交(需求 3.1.5)

```
A(房主)                     服务端                        B(最早加入)
 │ POST /leave ────────────▶│ 移除 A,广播 member_left         │
 │ ◀──────── 200 ───────────│ host=A → 移交 → host=B          │
 │                          │──── host_changed(B) ────────▶│
 │(WS 关闭)                  │──── host_changed(B) ────────▶│(其余成员)
```

### 5.4 掉线重连(需求 3.7)

```
B(成员,断网)                 服务端
 │  ✕ 连接断开                │ 90s 内:保留成员资格(占名额)      │
 │  指数退避重连…             │ >90s:心跳超时移除,广播 member_left │
 │ WS connect + auth(token)─▶│ 恢复身份(joined_at 沿用)         │
 │ ◀─── auth_ok(全量快照) ────│ 广播 member_joined              │
 │  快照整体覆盖本地 → 校准追赶   │
```

### 5.5 解散(需求 3.1.6)

```
A(房主)                     服务端                        全体
 │ POST /dissolve ─────────▶│ 广播 room_dissolved             │
 │ ◀──────── 200 ───────────│ 关闭全部 WS(close 4005)          │
 │                          │ 立即删视频文件 + SQLite 行 + 内存房间│
```
