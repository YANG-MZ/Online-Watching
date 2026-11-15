# movie-togher · 多人在线同步观影平台

基于 Web 的多人同步观影应用:创建观影房间,通过邀请链接邀请朋友;房主上传本地 MP4 视频,所有人的播放器实时跟随房主的播放、暂停、进度与倍速;每个人的音量、静音互不影响。

- 后端:Python 3.12 + FastAPI(WebSocket 信令 + HTTP Range 视频拉流 + SQLite 元数据)
- 前端:Vue 3 + Vite + TypeScript(Pinia + vue-router)
- 部署:单实例,优先本地笔记本电脑,局域网分享(需求 V1.2 / design T1.1)

> 详细需求与技术方案见 `doc/`(requirements / design / api / sync-protocol / implementation)。

---

## 一、环境要求

| 组件 | 版本 | 用途 |
| --- | --- | --- |
| Python | 3.12+ | 后端运行时 |
| [uv](https://docs.astral.sh/uv/) | 最新 | Python 依赖管理(本项目强制使用 uv) |
| Node.js / npm | 18+(实测 v24) | 前端构建与依赖管理 |
| 浏览器 | 桌面 Chrome / Edge | V1 优先适配 |

局域网联调时,请确保各设备与主机在同一网段。

---

## 二、获取与安装

```bash
# 1. 进入项目目录
cd movie-togher

# 2. 后端:安装依赖(uv 会自动创建 .venv)
uv sync

# 3. 前端:安装依赖
cd web
npm install
cd ..
```

---

## 三、开发模式(本机调试)

开两个终端:

```bash
# 终端 1:后端(端口 8000,带热重载)
uv run uvicorn server.app.main:app --reload --port 8000

# 终端 2:前端(端口 5173,Vite 已将 /api、/ws、/media 代理到 8000)
cd web
npm run dev
```

浏览器打开 <http://localhost:5173> 即可使用(开发环境无跨域问题)。

---

## 四、局域网部署(笔记本作为服务器)

### 步骤 1:构建前端(注入后端地址)

前端页面需要知道后端地址,构建时通过 `VITE_API_BASE_URL` 注入。

```bash
cd web

# Git Bash / macOS / Linux
VITE_API_BASE_URL="http://<本机局域网IP>:8000" npm run build

# Windows PowerShell
$env:VITE_API_BASE_URL="http://<本机局域网IP>:8000"; npm run build
```

`<本机局域网IP>` 换成你的局域网 IP(如 `192.168.1.10`;Windows 可用 `ipconfig` 查看)。
构建产物在 `web/dist/`。

### 步骤 2:托管前端静态文件(端口 8001,任选其一)

```bash
# 方案 A:npx serve(零配置)
cd web
npx serve dist -l 8001

# 方案 B:nginx(已安装时)
# server { listen 8001; root <项目>/web/dist; location / { try_files $uri /index.html; } }
```

### 步骤 3:启动后端(端口 8000)

```bash
# 在项目根目录执行
uv run uvicorn server.app.main:app --host 0.0.0.0 --port 8000
```

> ⚠️ **workers 必须为 1**(默认即 1,不要加 `--workers`):房间在线状态保存在进程内存,多进程不共享(design.md 4.6 硬约束)。

### 步骤 4:防火墙放行(仅局域网)

Windows 首次启动会弹防火墙提示,勾选"专用网络";或手动放行 8000/8001 两个端口。

### 步骤 5:分享

把 `http://<本机局域网IP>:8001` 发给同一局域网的朋友,他们浏览器打开即进入首页。

> 💡 建议将笔记本电源计划设为"接通电源时永不休眠"——合盖/休眠 = 服务中断,客户端会在服务恢复后自动重连(无需人工干预),但正在观看会被打断。

---

## 五、使用流程

1. **创建房间**:输入昵称 → 「创建房间」→ 系统生成房间 ID 与邀请链接,创建者即房主;
2. **邀请加入**:点击「复制邀请链接」发给朋友;朋友打开链接、输入昵称即可进入(无需注册);
3. **上传视频**(仅房主):选择本地 MP4 文件(H.264 + AAC,≤4 GB)→ 上传,进度条可见;完成后所有成员自动加载播放器;
4. **同步观看**(仅房主操作):播放 / 暂停 / 拖动进度条(松手生效)/ 调整倍速,全员实时跟随;
5. **个人偏好**(每人独立):音量滑条、静音、全屏——只影响自己,刷新后保留;
6. **中途加入**:新成员随时通过链接进入,自动定位到当前播放进度;
7. **离开与解散**:成员点「退出房间」;房主退出后房主身份自动移交给最早加入的成员;房主可「解散房间」(全员移出、视频立即删除)。

**房间生命周期**:全员离开 10 分钟后房间与视频自动清理;房主主动解散则立即清理;后端重启后历史房间失效(重启时自动清理残留文件)。

---

## 六、配置(config.toml)

配置文件位于项目根目录 `config.toml`,**修改后重启服务生效**。

| 配置节 | 关键项 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `[server]` | host / port | 0.0.0.0 / 8000 | 局域网访问需 0.0.0.0 |
| `[frontend]` | base_url | 空 | 填前端地址后,邀请链接返回绝对地址 |
| `[cors]` | allowed_origins | ["*"] | 分离部署建议收紧为实际前端地址 |
| `[limits]` | max_rooms | 5 | 最大房间数(超出拒绝创建) |
| | max_members_per_room | 10 | 每房间最大**在线**人数 |
| | max_upload_bytes | 4 GiB | 上传大小上限 |
| `[room]` | empty_expire_seconds | 600 | 全员离开后过期清理时长 |
| `[heartbeat]` | client_ping_seconds / server_timeout_seconds | 30 / 90 | 心跳与判死 |
| `[cleanup]` | scan_interval_seconds | 60 | 清理扫描周期 |
| `[storage]` | data_dir | "data" | SQLite、视频、日志根目录 |
| `[playback]` | allowed_rate_min / max | 0.25 / 3.0 | 倍速服务端校验范围 |
| `[log]` | level | info | 日志级别 |

**环境变量覆盖**(优先级高于文件):`MOVIE_TOGETHER__<节>__<键>`,例如
`MOVIE_TOGETHER__LIMITS__MAX_ROOMS=8`;配置文件路径可用 `MOVIE_TOGETHER_CONFIG` 指定。

---

## 七、数据目录

```
data/
├── app.db      # SQLite(房间/视频元数据,供清理任务追溯)
├── videos/     # 视频文件(UUID 重命名存储)
├── tmp/        # 上传中临时文件(中断即删,超龄兜底)
└── logs/       # 运行日志(轮转)
```

`data/` 已加入 `.gitignore`;**后端每次重启会清空其中全部房间与视频数据**(V1 接受的行为,需求 4.5)——需要长期保留的视频请在上传前自行备份。

---

## 八、测试

```bash
# 后端(108 项)
uv run pytest server/tests/

# 前端(Vitest 29 项)
cd web
npm test

# 前端类型检查 + 生产构建
npm run build
```

---

## 九、已知限制(V1)

| 限制 | 说明 |
| --- | --- |
| 仅校验 `.mp4` 扩展名 | 非 H.264/AAC 编码的 MP4 可能无法播放 |
| moov atom 在文件尾的 MP4 | 首次加载需先读取文件尾,大文件起播偏慢 |
| 自动播放策略 | 播放被拦截时页面会出现「点击画面开始播放」浮层 |
| 清除浏览器缓存 | 会丢失身份凭证(含房主身份),重新加入即可 |
| 服务器带宽 | 带宽需求 ≈ Σ(在线人数 × 码率),多人观看建议有线网络 |
| 无断点续传 | 上传中断需整体重传 |

---

## 十、目录结构

```
├── config.toml          # 运行配置
├── server/app/          # 后端(FastAPI:api / ws / room_manager / playback / db / video_storage / cleanup …)
├── server/tests/        # 后端 pytest
├── web/src/             # 前端(Vue 3:views / components / stores / api / sync)
├── doc/                 # 需求、技术方案、接口协议、同步算法、实现进度
└── data/                # 运行时数据(自动生成,gitignore)
```
