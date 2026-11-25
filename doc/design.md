# 多人在线同步观影平台 总体技术方案

| 项目名称 | movie-togher |
| --- | --- |
| 文档版本 | T1.2 |
| 更新日期 | 2026-11-15 |
| 需求依据 | doc/requirements.md(V1.3,唯一需求依据) |
| 配套文档 | doc/api.md(接口与 WS 协议)、doc/sync-protocol.md(播放同步协议) |
| 阶段 | 技术方案设计,不含业务代码 |

---

## 1. 文档说明

本方案以 doc/requirements.md(V1.2)为唯一需求依据,不改动作何已确认的业务规则。需求中存在歧义或需技术选型之处,已在设计前经用户确认,决策记录见第 10 节。

本阶段交付三份文档:

- **design.md**(本文):系统架构、模块设计、数据模型、配置、部署、测试策略;
- **api.md**:HTTP API 与 WebSocket 消息协议;
- **sync-protocol.md**:播放同步机制、时钟偏差处理与校准算法。

---

## 2. 系统架构

### 2.1 架构总览

```
                        ┌──────────────────────────────────────┐
                        │        笔记本 / 单台服务器            │
                        │                                      │
┌──────────┐  HTTP API  │  ┌──────────────────────────────┐    │
│ 浏览器 A │ ──────────▶ │  │   后端 FastAPI(单进程)      │    │
│ (房主)   │ ◀────────── │  │   uvicorn workers=1          │    │
│          │  WebSocket │  │                              │    │
│  Vue 3   │ ◀────────▶ │  │  ┌────────┐  ┌───────────┐  │    │
│  SPA ×N  │            │  │  │ HTTP   │  │ WS 端点    │  │    │
│(≤10人/房)│  HTTP      │  │  │ API    │  │ (房间信令) │  │    │
│          │  Range     │  │  └────────┘  └───────────┘  │    │
│          │ ──────────▶ │  │  ┌──────────────────────┐  │    │
│          │  视频拉流   │  │  │ RoomManager(内存)    │  │    │
│          │            │  │  │ 权威播放状态/成员/心跳  │  │    │
│          │            │  │  └──────────────────────┘  │    │
│          │            │  │  ┌────────┐ ┌───────────┐  │    │
└──────────┘            │  │  │ SQLite │ │ 视频文件目录│  │    │
       ▲                │  │  │(元数据)│ │ (UUID.mp4) │  │    │
       │ 静态资源        │  │  └────────┘ └───────────┘  │    │
       │(JS/CSS/HTML)   │  │  清理任务(后台协程)        │    │
       └─────────────── │  └──────────────────────────────┘    │
                        │  ┌──────────────────────────────┐    │
                        │  │ 前端静态服务(独立部署单元)    │    │
                        │  │ nginx / 任意静态服务器        │    │
                        │  └──────────────────────────────┘    │
                        └──────────────────────────────────────┘
```

要点:

- **控制信令**(播放/暂停/跳转/倍速/成员变动)走 WebSocket;
- **视频数据**走独立 HTTP Range 请求,与 WS 完全分离(需求 3.6.1);
- **房间在线状态与播放权威状态**仅存内存(需求 4.5),SQLite 只存元数据供清理任务追溯;
- 前后端**分离部署**(用户决策 T4),各自独立进程/端口。

### 2.2 部署视图

| 环境 | 前端 | 后端 | 说明 |
| --- | --- | --- | --- |
| 开发 | Vite dev server(5173) | uvicorn(8000) | Vite proxy 将 `/api`、`/ws` 转发至 8000,无跨域 |
| 笔记本运行 | 静态服务器托管 `web/dist`(如 nginx 8001 或 `npx serve`) | uvicorn 8000 | 后端开启 CORS 允许前端 origin;前端构建时注入后端地址 `VITE_API_BASE_URL` |

- 局域网用户访问前端地址(如 `http://<笔记本IP>:8001`),页面再向后端地址(`http://<笔记本IP>:8000`)发起 API/WS 请求;
- WebSocket 不受浏览器同源策略约束,无需 CORS,直接连后端;
- 两端口需在防火墙放行(仅局域网)。

### 2.3 关键数据流

1. **建房/加入**:HTTP 创建/加入 → 签发凭证 → 前端连 WS → 首条 `auth` → 服务端校验并加入在线成员 → 回 `auth_ok`(含全量快照)→ 广播 `member_joined`;
2. **上传**:房主 PUT 原始字节流(带进度)→ 服务端流式写临时文件 → 原子重命名为 UUID.mp4 → 写 SQLite → 广播 `video_ready`/`video_changed`;
3. **观看**:各成员浏览器独立向后端发 Range 请求拉流;
4. **同步**:房主 WS 发控制消息 → 服务端更新权威状态(version+1)→ 广播 `state_update` → 各端按 sync-protocol 校准;
5. **清理**:后台协程周期扫描,处理空房过期、解散残留、上传临时文件;进程启动时清理全部残留(重启即房间失效)。

---

## 3. 技术栈与选型决策

| # | 层面 | 选型 | 备选 | 理由 |
| --- | --- | --- | --- | --- |
| T1 | 后端框架 | FastAPI + uvicorn(**workers=1**) | — | 需求第 5 节已定;内存态强制单进程单事件循环 |
| T2 | Python 版本 | 3.12(仓库 `.python-version` 已定) | — | — |
| T3 | 数据库访问 | 标准库 `sqlite3` + WAL 模式,写入经 `asyncio.to_thread` | aiosqlite、SQLModel | 元数据写入频率极低(建房/上传/清理),无 ORM 必要,零依赖 |
| T4 | 部署形态 | **前后端分离部署**(用户确认) | 后端托管前端静态文件 | 用户决策;后端需配置 CORS |
| T5 | 前端构建 | Vue 3 + Vite + TypeScript(用户决策,2026-11-15,见 T17) | — | 组合式 API;Vite 开发体验好 |
| T6 | 前端状态 | Pinia(持久化用 pinia-plugin-persistedstate) | 组合式 API 手写 store | 官方推荐;localStorage 持久化开箱即用(音量/静音/会话) |
| T7 | 前端路由 | vue-router | 手写路由 | 官方配套,两个路由也低成本 |
| T8 | 上传协议 | `PUT` 原始字节流 + `X-Filename` 头,前端 XHR(`upload.onprogress`) | multipart/form-data | 流式落盘零解析开销;fetch 无上传进度,XHR 有;Content-Length 可预检大小上限 |
| T9 | 视频下发 | 手写 Range 处理(206/416/HEAD) | Starlette FileResponse 内建 Range | 行为不与框架版本耦合,边界完全自控、可测 |
| T10 | 配置 | `config.toml` + 内置 `tomllib` 读取 + 环境变量覆盖 | YAML、.env | Python 3.11+ 内置解析零依赖;可读性好,满足"配置文件可修改"(验收 17) |
| T11 | 日志 | loguru | stdlib logging | 小项目配置成本低 |
| T12 | WS 鉴权 | 连接后**首条消息 auth** | URL query 携带 token | 浏览器 WS 无法自定义 header;query 有进访问日志的泄漏风险 |
| T13 | 前端测试 | Vitest(可选,核心同步逻辑单测) | — | V1 以 pytest 后端测试 + 手动验收为主 |

---

## 4. 后端设计

### 4.1 目录结构(monorepo)

```
├── pyproject.toml          # uv 项目(已有,后端依赖在此管理)
├── config.toml             # 运行配置(见第 7 节)
├── server/
│   ├── app/
│   │   ├── main.py         # FastAPI 装配、CORS、生命周期(lifespan 启动清理+后台任务)
│   │   ├── config.py       # 配置加载(pydantic 校验 + tomllib + 环境变量覆盖)
│   │   ├── models.py       # 内存领域模型(Room/Member/PlaybackState/VideoMeta)
│   │   ├── room_manager.py # 房间生命周期、成员增删、满员、移交、复活
│   │   ├── playback.py     # 权威播放状态更新(结算规则见 sync-protocol.md)
│   │   ├── api.py          # HTTP 端点(见 api.md)
│   │   ├── ws.py           # WS 端点、连接管理、消息路由、心跳扫描
│   │   ├── video_storage.py# 上传写盘、Range 响应、文件删除
│   │   ├── db.py           # SQLite DAO(rooms/videos)
│   │   ├── cleanup.py      # 后台清理协程
│   │   └── security.py     # ID/token 生成(secrets)、token 校验
│   └── tests/              # pytest
├── web/                    # 前端(Vue 3 + Vite + TS)
│   ├── src/
│   │   ├── main.ts         # 应用入口(createApp + Pinia + Router)
│   │   ├── App.vue         # 根组件(<router-view>)
│   │   ├── api/http.ts     # HTTP 客户端
│   │   ├── api/ws.ts       # WS 客户端(心跳+指数退避重连+踢旧处理)
│   │   ├── sync/           # 时钟估计、同步引擎、校准器(sync-protocol.md 的实现)
│   │   ├── stores/         # Pinia:session / room / preference / upload
│   │   ├── views/Home.vue  # 创建/加入
│   │   ├── views/Room.vue  # 房间页
│   │   └── components/     # VideoPlayer、MemberList、UploadPanel 等(.vue 单文件组件)
│   └── package.json
└── data/                   # 运行时目录(gitignore):db + videos/ + tmp/
```

### 4.2 内存领域模型

```python
class PlaybackState:          # 权威播放状态(需求 3.4.1)
    state: Literal["playing", "paused"]
    position: float           # 秒,最近一次 updated_at 时刻的位置
    rate: float               # 倍速
    updated_at: float         # 服务端单调时钟(time.time())
    version: int              # 单调递增

class Member:                 # 在线成员(有活跃 WS)
    user_id: str
    nickname: str
    joined_at: float          # 首次加入时刻(重连沿用,用于移交排序)
    conn: WebSocket | None    # 活跃连接;被踢/断开后置空
    last_seen: float          # 任意 WS 消息均刷新

class VideoMeta:
    video_id: str             # UUID,文件名 = video_id + ".mp4"
    original_name: str
    size: int

class Room:
    room_id: str
    host_user_id: str
    members: dict[str, Member]        # 在线成员(user_id → Member)
    known_users: dict[str, float]     # 历史加入者 user_id → joined_at(掉线移除后保留,供重连沿用 joined_at 与移交排序)
    playback: PlaybackState
    video: VideoMeta | None
    uploading: bool                   # 上传互斥锁
    empty_since: float | None         # 在线成员变空的时刻;非空为 None
    created_at: float
```

- `known_users` 随房间存续保留,房间清理时一并销毁;数据量 ≤ 上限人数,无内存压力;
- 房主身份 = `room.host_user_id`,由服务端维护(需求 3.2.1),不写入 token。

### 4.3 房间生命周期状态机

```
                    创建(房数<max_rooms,写 DB)
  ┌─────────┐   ┌───────────────────────────────┐
  │ 不存在   │──▶│ 活跃(members 非空)             │
  └─────────┘   └───────────────────────────────┘
                     │ 在线成员变空(主动退出移除/心跳超时移除)
                     ▼
                ┌───────────────────────────────┐
                │ 空房倒计时(empty_since=now)    │
                └───────────────────────────────┘
                     │                    │
        有人 WS auth │                    │ now-empty_since ≥ 10min
        (复活:取消计时)│                    ▼
                     └──── 活跃 ◀┘    ┌────────────┐
                                       │ 清理删除     │── 房间+视频文件+DB 行
                                       └────────────┘
  任意状态:房主解散 ──────────────────▶ 清理删除(立即)
  进程重启:内存全部丢失 ──启动清理──▶ DB 行与文件全部删除
```

规则落地(全部来自需求 3.1.7,含已确认决策):

- **移除时机**:主动退出(leave HTTP)立即移除;掉线由心跳扫描协程移除(`now - last_seen > 90s`);
- **空房复活**(决策 T14):空房倒计时期间,任何用户(含凭旧 token 的原成员)WS auth 成功即取消 `empty_since`;
- **过期判定**:仅清理任务执行,判定与删除在同一事件循环串行段内完成,无竞态;
- **无绝对时长上限**(需求 3.1.7)。

### 4.4 成员与在线管理

1. **进入房间以 WS auth 为准**:HTTP join 仅校验房间存在与昵称合法性并签发凭证,不占名额;WS auth 成功才加入 `members` 并广播。满员校验(在线人数 ≥ `max_members_per_room`,决策 T13)在 WS auth 时执行,失败以 close code 4002 拒绝,前端展示"房间已满"。
2. **同 token 多连接**(决策 T15,踢旧):auth 时发现该 user 已有活跃连接 → 向旧连接发 `kicked{reason:"duplicate_connection"}` 并关闭(close 4004),新连接接管,`joined_at` 沿用 `known_users` 记录,成员列表不重复(验收 8)。
3. **心跳**:客户端每 30s 发 `ping`;服务端对任意消息刷新 `last_seen`;扫描协程每 15s 检查,超 90s 判死(需求 3.6.2),关闭连接、移出成员、广播 `member_left{reason:"timeout"}`。
4. **成员变空钩子**:每次移除成员后,若 `members` 为空则记 `empty_since = now`;有成员加入则置 None。
5. 昵称允许重复(需求 3.1.3),界面以昵称展示、user_id 标识。

### 4.5 房主移交算法

仅房主**主动退出**触发(需求 3.1.5):

```
on_host_leave(room):
    candidates = members 中除房主外的成员,按 joined_at 升序
    if candidates 非空:
        room.host_user_id = candidates[0].user_id
        广播 host_changed{host_user_id}
    else:
        不移交(房间进入空房倒计时或直接清理)
```

- 房主掉线(心跳超时)不移交:`host_user_id` 不变,重连 auth 后自然恢复控制权(验收 14);
- 房主主动退出后凭原 token 再加入:此时 `host_user_id` 已变更或房间已清理,其身份为普通成员(需求 3.1.5);
- 解散与移交互斥:解散时全员移出,无移交。

### 4.6 并发模型与约束

- **uvicorn workers=1**(硬约束):房间状态只在进程内存,多 worker 不共享;文档与启动脚本均注明;
- 所有房间状态变更(HTTP 回调、WS 消息、心跳扫描、清理任务)都在**同一 asyncio 事件循环**内串行执行,天然无锁;
- 阻塞 IO(SQLite 写、文件写盘/删除)经 `asyncio.to_thread` 下放线程池,避免卡顿事件循环;
- 视频拉流(StreamingResponse)为异步生成器分块读文件(1 MiB/块),不阻塞事件循环。

### 4.7 视频上传流程(需求 3.3)

```
PUT /api/rooms/{room_id}/video
  预检(顺序):
    1 Authorization 房主校验(403)
    2 X-Filename 解码后扩展名 == .mp4(否则 400 INVALID_FORMAT)
    3 Content-Length ≤ max_upload_bytes(否则 413 FILE_TOO_LARGE)
    4 房间无进行中上传(否则 409 UPLOAD_IN_PROGRESS)
  写盘:
    5 流式写 data/tmp/{upload_id}.part,累计计数再次校验大小(防 chunked 无 CL)
    6 每写 64 MiB 复查一次"请求发起者仍为房主且房间未解散",否则中止删临时(403/404)
  完成:
    7 fsync → 原子 rename 为 data/videos/{uuid}.mp4 → 写 SQLite videos 行
    8 若替换:删旧文件与旧行(需求 D4)
    9 权威状态重置:state=paused, position=0, rate=1.0, version+1
   10 广播 video_ready(首次)或 video_changed(替换)
```

- **进度**:前端 XHR `upload.onprogress`(本地统计已发字节,不依赖服务端);
- **中断**:连接断开即删除 `.part`;漏删的由清理任务按 `tmp` 超龄兜底(需求 4.5);
- **失败提示**:错误码结构化返回(api.md 第 4 节),前端按 code 展示明确文案。

### 4.8 视频下发(Range)

- `GET /media/{video_id}.mp4`(video_id 为 UUID,URL 不可猜测即最低访问控制,需求 4.3;不做逐请求鉴权);
- 支持:`Range: bytes=start-[end]` 单段(浏览器行为)→ `206` + `Content-Range` + `Accept-Ranges: bytes`;无 Range → `200` 全量;`HEAD` 返回头;起止非法 → `416`;
- 分块异步流式输出;`Content-Type: video/mp4`;V1 不做 ETag/304;
- 不做 faststart、不转码(需求 7.1/7.2 已知限制)。

### 4.9 清理任务

单协程,周期 `cleanup.scan_interval_seconds`(默认 60s):

1. **空房过期**:内存房间 `empty_since` 非空且超时 → 删视频文件 + SQLite 行 + 内存对象;
2. **上传临时文件**:`data/tmp/*.part` 超过 `temp_file_max_age_seconds`(默认 1h)→ 删除;
3. **孤儿文件兜底**:DB 无记录但存在于 `videos/` 的文件(极端崩溃残留)→ 删除。

**进程启动清理**(lifespan 内执行):内存房间为空,故 DB 中所有 rooms/videos 行均为残留 → 全部删除(需求 4.5:重启后房间失效,文件按元数据清理)。

### 4.10 标识与凭证生成(需求 4.3,全部 `secrets` 模块密码学随机)

| 标识 | 格式 | 示例 |
| --- | --- | --- |
| room_id | 8 位大写 Crockford Base32(去除易混字符 0/O/1/I) | `7QXK4M9R` |
| user_id | 16 位十六进制 | `a3f8c92e11b04d77` |
| session_token | `secrets.token_urlsafe(32)`(43 字符) | — |
| video_id | UUID4 hex(32 字符) | — |

- session_token 服务端仅在内存中比对(房间对象内),不落库、不记日志;
- 邀请链接格式:`{前端 baseURL}/rooms/{room_id}`(前端路由)。

### 4.11 权限矩阵(需求 3.2.2)

| 操作 | 校验位置 | 普通成员/无 token | 房主 | 未上线(token 有效) |
| --- | --- | --- | --- | --- |
| 创建/加入房间 | HTTP | 允许(无需 token) | — | — |
| WS auth(进入房间) | WS 首条消息 | — | — | 允许,恢复身份 |
| 播放/暂停/seek/倍速 | WS 消息 | 拒绝 `error{FORBIDDEN}` | 允许 | —(不在线即无消息) |
| 上传/替换 | HTTP | 403 | 允许 | — |
| 解散 | HTTP | 403 | 允许 | — |
| 主动退出 | HTTP | 允许(自己的身份) | 允许(触发移交) | — |

---

## 5. 数据模型(SQLite)

仅存清理追溯所需最小集(需求 D3),成员与播放状态不落库。

```sql
-- db/versioning:PRAGMA user_version = 1
CREATE TABLE IF NOT EXISTS rooms (
    room_id      TEXT PRIMARY KEY,
    created_at   REAL NOT NULL,            -- unix 秒
    host_user_id TEXT NOT NULL             -- 仅追溯用,重启不恢复
);

CREATE TABLE IF NOT EXISTS videos (
    video_id      TEXT PRIMARY KEY,        -- 即存储文件名主干
    room_id       TEXT NOT NULL,
    original_name TEXT NOT NULL,
    size          INTEGER NOT NULL,
    created_at    REAL NOT NULL,
    file_path     TEXT NOT NULL            -- 相对 data 目录
);

CREATE INDEX IF NOT EXISTS idx_videos_room ON videos(room_id);
```

**读写时机**:

| 事件 | 操作 |
| --- | --- |
| 创建房间 | INSERT rooms |
| 上传完成 | INSERT videos(替换:先删旧文件与旧行再插入新行,同一线程段内完成) |
| 房间过期/解散/启动清理 | DELETE videos → DELETE rooms,先删物理文件再删行 |
| 其余全部(成员/播放/心跳/移交) | 仅内存 |

连接参数:`check_same_thread=False` + WAL;所有 DB 调用经 `asyncio.to_thread`。

---

## 6. 前端设计

### 6.1 页面与路由

| 路由 | 页面 | 内容 |
| --- | --- | --- |
| `/` | Home | 昵称输入;「创建房间」;「通过链接/房间 ID 加入」;错误提示(房间不存在/已满/达到上限) |
| `/rooms/:roomId` | Room | 播放器、控制条(播放/暂停/进度条/倍速,**成员端只读**)、成员列表(房主标记)、上传面板(房主,进度条+错误文案)、邀请链接复制、退出按钮、解散按钮(房主,二次确认)、连接状态指示(在线/重连中/已断开)、被踢/解散通知浮层 |

### 6.2 状态管理(Pinia)

| store | 内容 | 持久化 |
| --- | --- | --- |
| `sessionStore` | room_id、user_id、session_token、昵称、is_host | localStorage(需求 3.2.1,刷新恢复身份) |
| `roomStore` | 快照(members/host/playback/video)、连接状态、错误通知 | 否(以服务端快照为准) |
| `preferenceStore` | volume、muted(独立偏好,不发送服务端,需求 3.5) | localStorage |
| `uploadStore` | 进度、状态、错误 | 否 |

- 全屏为元素级状态,不入 store;**全屏偏好持久化的浏览器限制**:进入全屏必须由用户手势触发,故刷新后无法自动恢复全屏(音量/静音可自动恢复)。前端记录偏好值,刷新后提示用户点击恢复——此为需求 3.5 在浏览器安全模型下的落地方式,不改变"全屏独立、互不影响"的规则。

### 6.3 WS 客户端与重连(需求 3.7)

- 鉴权:连接后立即发 `auth{token}`;10s 未收到 `auth_ok` 视为失败;
- **重连**:指数退避 1s→2s→4s→…→上限 30s,成功后重置;每次重连成功经 `auth_ok` 获取**全量快照**并整体覆盖本地状态(不依赖断线期间事件);
- 重连期间:本地播放不主动中断(UI 显示"重连中"),快照到达后由校准器收敛;
- 房间不存在/已解散(close 4003/4005):停止重连,展示终态提示;
- 被踢(duplicate_connection,4004):当前页面停止一切活动并提示"已在其他窗口打开";
- 心跳:每 30s 发 `ping{t0}` 并记录 RTT/时钟偏差样本(sync-protocol.md §3)。

### 6.4 播放器与同步引擎

- HTML5 `<video>`,`src=/media/{video_id}.mp4`(经代理或直连后端);
- 同步引擎(独立模块,见 sync-protocol.md):事件应用 + 周期校准;房主端 UI 操作仅发送 WS 控制消息,**本地不直接生效**(以服务端回广播为准,保证与成员端同路径,消除双路径分叉);
- 拖动进度条:`input` 事件仅更新 UI,`change`(松手/drag end)才发 `seek`(需求 3.4.2);
- autoplay 拦截处理(需求 7.3):`play()` 返回的 Promise 被 reject 时,展示"点击画面开始播放"浮层,点击后重试并继续跟随同步;
- 个人音量/静音直接作用于 `<video>` 元素,与同步引擎完全隔离(需求 3.5)。

---

## 7. 配置设计(config.toml)

文件位于仓库根(路径可经环境变量 `MOVIE_TOGETHER_CONFIG` 覆盖);pydantic 模型校验类型与下限;环境变量覆盖规则 `MOVIE_TOGETHER__<节>__<键>`。

```toml
[server]
host = "0.0.0.0"          # 监听地址(局域网访问需 0.0.0.0)
port = 8000

[frontend]
base_url = ""             # 前端地址,拼接邀请链接;空 = 相对路径由前端补全
serve_dist = false        # 单 URL 部署(T18):true = 后端托管前端产物,单进程单端口
dist_dir = "web/dist"     # 前端构建产物目录

[cors]
allowed_origins = ["*"]   # 前端 origin 列表;分离部署下按实际前端地址收紧,如 ["http://192.168.1.10:8001"]

[limits]
max_rooms = 5                      # 验收 16/17
max_members_per_room = 10          # 在线口径(决策 T13)
max_upload_bytes = 4294967296      # 4 GiB

[room]
empty_expire_seconds = 600         # 全员离开 10 分钟(需求 3.1.7)

[heartbeat]
client_ping_seconds = 30
server_timeout_seconds = 90
scan_interval_seconds = 15

[cleanup]
scan_interval_seconds = 60
temp_file_max_age_seconds = 3600

[storage]
data_dir = "data"          # SQLite 与视频文件根目录(相对工作目录)

[playback]
allowed_rate_min = 0.25    # 倍速服务端校验范围(前端 UI 档位 0.5~2.0)
allowed_rate_max = 3.0

[log]
level = "info"
```

配置读取发生在启动时;修改后重启生效(与验收 17 一致)。

---

## 8. 部署方案

### 8.1 开发环境

```bash
# 后端
uv sync && uv run uvicorn server.app.main:app --reload --port 8000
# 前端(web/,Vite proxy 已将 /api、/ws、/media 转发至 127.0.0.1:8000)
npm install && npm run dev
```

### 8.2 笔记本运行(分离部署)

```bash
# 后端
uv run uvicorn server.app.main:app --host 0.0.0.0 --port 8000   # workers 固定为 1
# 前端:构建时注入后端地址
cd web && VITE_API_BASE_URL="http://<笔记本局域网IP>:8000" npm run build
# 静态托管 dist(任选其一)
npx serve dist -l 8001        # 轻量方案
# 或 nginx: root 指向 dist,try_files 回退 index.html(SPA 路由)
```

- 分享地址:`http://<笔记本IP>:8001`;
- 防火墙放行 8000/8001(仅局域网网段);
- 笔记本合盖/休眠 = 服务中断,客户端自动重连恢复(需求第 2 节);建议电源计划设置为"接通电源时不休眠"。

---

## 9. 测试策略与验收映射

后端 pytest(FastAPI `TestClient` 原生支持 WS 多连接仿真),前端核心同步逻辑 Vitest 单测,其余手动验收。

| 验收条目 | 验证方式 |
| --- | --- |
| 1 多浏览器进同一房间 | 手动 + WS 仿真测试 |
| 2 视频可播放且 Range 生效 | pytest:206/Content-Range/分片请求 |
| 3/4/5 播放/暂停/跳转/倍速同步 | pytest:WS 双连接,房主发消息断言广播与状态结算 |
| 6 音量/静音独立且持久 | 手动(前端行为)+ Vitest preference store(Pinia) |
| 7 中途加入定位进度 | pytest:auth_ok 快照推算位置正确 |
| 8 断网重连不重复 | pytest:断开重连后 members 去重、joined_at 沿用 |
| 9 越权被拒(403) | pytest:普通成员/伪造 token 调房主接口 |
| 10 全员离开 10 分钟清理 | pytest:时间 mock 触发清理协程 |
| 11 非法文件/超限被拒 | pytest:扩展名、Content-Length、超量流 |
| 12 替换视频重置并广播 | pytest:video_changed + 状态重置 + 旧文件删除 |
| 13 主动退出移交最早成员 | pytest:多人按序加入,房主退出断言 host_changed |
| 14 掉线不移交、重连恢复 | pytest:模拟心跳超时 |
| 15 解散立即清理 | pytest:广播 room_dissolved、文件与行删除 |
| 16 上限拒绝 | pytest:第 6 房、第 11 人 |
| 17 配置修改生效 | pytest:覆盖配置启动 |
| 8.2-1 操作生效 ≤1s | 手动(局域网实测)+ WS 广播路径无阻塞设计评审 |
| 8.2-2 稳态偏差 ≤0.5s | 手动:两端对比播放时间戳;校准器 Vitest 单测 |

---

## 10. 设计决策记录(2026-11-14,经用户确认)

| # | 议题 | 结论 |
| --- | --- | --- |
| T13 | 满员计数口径 | 按**在线成员数**(心跳超时移出即释放名额;掉线未超时期间仍占名额) |
| T14 | 空房复活 | **允许**:10 分钟窗口内 WS auth 成功即取消过期计时 |
| T15 | 同 token 第二条 WS | **新连接踢旧连接**(kicked/duplicate_connection),身份单活,joined_at 沿用 |
| T16 | 前端部署形态 | **前后端分离部署**(后端 CORS + 前端构建注入后端地址) |
| T17 | 前端框架变更(2026-11-15) | **Vue 3 + Vite + TypeScript**(用户决策,替代 T5 原推荐 React);T6 状态管理随之改为 Pinia、T7 路由改为 vue-router;后端与协议不变 |
| T18 | 单 URL 部署(2026-11-15,N4) | **后端托管前端构建产物**(`[frontend].serve_dist` 开关,默认关闭):单进程单端口对外,同源无 CORS;SPA 回退(深链接返 index.html)、路径穿越防护、api/media/ws 前缀不被页面吞;分离部署形态保留可切换 |
| T19 | 房间密码(2026-11-15,N3) | 创建时可选设置(≤64 字符),**SHA-256 哈希存储**(rooms 表 + 内存 Room);密码校验仅在 **HTTP join**(通过即签发 token,token 为已验证门票,WS 重连沿用);错误码 PASSWORD_REQUIRED/PASSWORD_INCORRECT/INVALID_PASSWORD;WS auth 不重复校验密码 |

其余技术选型(第 3 节 T1~T12)为推荐方案,实现阶段如需替换不影响已确认业务规则。

## 11. 遗留事项

- doc/requirements.md 第 1 行大标题"V1.0"与头部表格版本不一致的排版笔误已随 V1.2 修正(2026-11-15);
- 全屏偏好的浏览器手势限制落地方式见 6.2,不改变需求 3.5 规则本身。
