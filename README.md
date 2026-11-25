# movie-togher · 多人在线同步观影平台

基于 Web 的多人同步观影应用:创建观影房间,通过邀请链接邀请朋友;房主上传本地 MP4 视频,所有人的播放器实时跟随房主的播放、暂停、进度与倍速;每个人的音量、静音互不影响。可选房间密码,支持**单 URL 部署**(一个进程一个端口)并可通过 Cloudflare Tunnel 安全暴露给外网朋友。

- 后端:Python 3.12 + FastAPI(WebSocket 信令 + HTTP Range 视频拉流 + SQLite 元数据)
- 前端:Vue 3 + Vite + TypeScript(Pinia + vue-router)
- 部署:单实例,优先本地笔记本电脑;支持分离部署或单 URL 部署(需求 V1.3 / design T1.2)

> 详细需求与技术方案见 `doc/`(requirements / design / api / sync-protocol / implementation)。

---

## 一、环境要求

| 组件 | 版本 | 用途 |
| --- | --- | --- |
| Python | 3.12+ | 后端运行时 |
| [uv](https://docs.astral.sh/uv/) | 最新 | Python 依赖管理(本项目强制使用 uv) |
| Node.js / npm | 18+(实测 v24) | 前端构建与依赖管理 |
| 浏览器 | 桌面 Chrome / Edge | V1 优先适配 |
| cloudflared(可选) | 最新 | 外网访问(Cloudflare Tunnel) |

---

## 二、获取与安装

```bash
# 1. 进入项目目录
cd movie-togher

# 2. 后端:安装依赖(uv 会自动创建 .venv)
uv sync

# 3. 前端:安装依赖并构建
cd web
npm install
npm run build      # 产物在 web/dist/
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

## 四、部署上线(单 URL 模式,推荐)

单 URL 模式下,后端一个进程(端口 8000)同时提供页面与接口——**只有一个地址需要分享**。

### 步骤 1:开启后端托管前端

编辑 `config.toml`:

```toml
[frontend]
serve_dist = true         # 由后端托管 web/dist
dist_dir = "web/dist"
```

### 步骤 2:启动服务

```bash
uv run uvicorn server.app.main:app --host 0.0.0.0 --port 8000
```

局域网朋友直接访问 `http://<本机局域网IP>:8000`。

> ⚠️ **workers 必须为 1**(默认即 1,不要加 `--workers`):房间在线状态保存在进程内存(design.md 4.6 硬约束)。

### 步骤 3(可选):外网朋友访问 —— Cloudflare Tunnel

不在同一局域网的朋友,通过 Cloudflare Tunnel 把本机服务安全暴露到公网(出站隧道,**路由器不开任何入站端口**,自动 HTTPS):

```bash
# 方式一:临时快速隧道(免账号;URL 每次启动随机变化,适合临时聚会)
cloudflared tunnel --url http://localhost:8000

# 方式二:命名隧道(URL 固定,需 Cloudflare 账号与域名,适合长期使用)
# 完整流程(Windows,示例域名 movie.o7o7.dpdns.org):
#   1) 域名接入 Cloudflare 后,在 DNS 页面点「继续前往激活」
#   2) 安装:winget install --id Cloudflare.cloudflared(装完重开终端)
#   3) 登录:cloudflared tunnel login(浏览器选择你的域名授权)
#   4) 创建:cloudflared tunnel create movie-togher(记下 Tunnel ID 与凭据文件路径)
#   5) 路由(自动创建 CNAME,无需在 DNS 页面手动添加):
#      cloudflared tunnel route dns -f movie-togher movie.o7o7.dpdns.org
#   6) 写 %USERPROFILE%\.cloudflared\config.yml:
#        tunnel: <Tunnel-ID>
#        credentials-file: C:\Users\<你>\.cloudflared\<Tunnel-ID>.json
#        ingress:
#          - hostname: movie.o7o7.dpdns.org
#            service: http://localhost:8000
#          - service: http_status:404
#   7) 启动:cloudflared tunnel run movie-togher
#   8) 验证:浏览器打开 https://movie.o7o7.dpdns.org/healthz
cloudflared tunnel run movie-togher
```

把得到的 `https://xxx` 地址发给朋友即可,**一个 URL 全部搞定**(页面、接口、视频流同源,无需 CORS 与第二个端口)。

> 替代方案:[Tailscale](https://tailscale.com/) 组网(朋友安装客户端,仅成员可见,安全性最高);frp 自建(需公网 VPS)。

### 步骤 4:防火墙

仅局域网访问时放行 8000(Windows 首次启动弹窗勾选"专用网络");使用 Cloudflare Tunnel 时**无需**开放任何入站端口(出站隧道)。

> 💡 建议笔记本电源计划设为"接通电源时永不休眠"——合盖/休眠 = 服务中断,客户端会在服务恢复后自动重连。

<details>
<summary>旧方式:前后端分离部署(仍支持)</summary>

构建时注入后端地址(`VITE_API_BASE_URL="http://<IP>:8000" npm run build`),`web/dist` 用 npx serve/nginx 单独托管(如 8001),`config.toml` 保持 `serve_dist = false`。适合前后端独立扩展的场景。

</details>

---

## 五、使用流程

1. **创建房间**:输入昵称 → 可选设置**房间密码**(≤64 字符;设置后加入必须输对密码)→ 「创建房间」,系统生成房间 ID 与邀请链接,创建者即房主;
2. **邀请加入**:复制邀请链接发给朋友(设置了密码请一并告知);朋友打开链接、输入昵称与密码进入(无需注册);密码校验通过后断线重连无需再输;
3. **上传视频**(仅房主):选择本地 MP4 文件(H.264 + AAC,≤4 GB)→ 上传,进度条可见;完成后所有成员自动加载播放器;
4. **同步观看**(仅房主操作):播放 / 暂停 / 拖动进度条(松手生效)/ 调整倍速,全员实时跟随;
5. **个人偏好**(每人独立):音量滑条、静音、全屏——只影响自己,刷新后保留;
6. **中途加入**:新成员随时进入,自动定位到当前播放进度;
7. **离开与解散**:成员点「退出房间」;房主退出后身份自动移交给最早加入的成员;房主可「解散房间」(全员移出、视频立即删除)。

**房间生命周期**:全员离开 10 分钟后房间与视频自动清理;房主主动解散立即清理;后端重启后历史房间失效(重启时自动清理残留文件与旧结构数据库)。

---

## 六、配置(config.toml)

配置文件位于项目根目录,**修改后重启服务生效**。

| 配置节 | 关键项 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `[server]` | host / port | 0.0.0.0 / 8000 | 局域网访问需 0.0.0.0 |
| `[frontend]` | base_url | 空 | 填前端地址后,邀请链接返回绝对地址 |
| | serve_dist / dist_dir | false / web/dist | 单 URL 部署(T18) |
| `[cors]` | allowed_origins | ["*"] | 分离部署建议收紧;单 URL 同源时无 CORS |
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

## 七、数据目录与下线

```
data/
├── app.db      # SQLite(房间/视频元数据,含密码哈希)
├── videos/     # 视频文件(UUID 重命名存储)
├── tmp/        # 上传中临时文件(中断即删,超龄兜底)
└── logs/       # 运行日志(轮转)
```

**下线(取消部署)**:停止后端(Ctrl + C 或结束进程)与前端托管进程即完成;`data/` 会保留到**下次启动**——下次启动时会自动清空全部房间与视频数据(需求 4.5),需要保留视频请在停止前复制 `data/videos/` 中的文件(原始文件名记录在 `data/app.db`)。

---

## 八、测试

```bash
# 后端(117 项)
uv run pytest server/tests/

# 前端(Vitest 29 项)
cd web
npm test

# 前端类型检查 + 生产构建
npm run build
```

---

## 九、安全性说明

| 层面 | 现状(V1.3) |
| --- | --- |
| 网络层 | 单 URL 部署 + Cloudflare Tunnel:路由器零入站暴露、自动 HTTPS;Tailscale 组网则仅成员可达 |
| 身份 | 服务端签发 `session_token`(密码学随机),房主标记由服务端维护 |
| 房间门槛 | 可选房间密码:SHA-256 哈希存储(不落明文),错误密码明确拒绝 |
| 视频 | UUID 文件名不可猜测;"持有 URL 即可访问"仍成立(请控制链接传播范围) |
| 越权 | 非房主调用房主接口一律服务端拒绝(403) |

---

## 十、已知限制(V1.3)

| 限制 | 说明 |
| --- | --- |
| 仅校验 `.mp4` 扩展名 | 非 H.264/AAC 编码的 MP4 可能无法播放 |
| moov atom 在文件尾的 MP4 | 首次加载需先读取文件尾,大文件起播偏慢 |
| 自动播放策略 | 播放被拦截时页面会出现「点击画面开始播放」浮层 |
| 清除浏览器缓存 | 会丢失身份凭证(含房主身份),重新加入即可 |
| 服务器带宽 | 带宽需求 ≈ Σ(在线人数 × 码率),多人观看建议有线网络 |
| 无断点续传 | 上传中断需整体重传 |

---

## 十一、目录结构

```
├── config.toml          # 运行配置
├── server/app/          # 后端(FastAPI:api / ws / room_manager / playback / db / video_storage / cleanup …)
├── server/tests/        # 后端 pytest
├── web/src/             # 前端(Vue 3:views / components / stores / api / sync)
├── web/dist/            # 前端构建产物(npm run build 生成)
├── doc/                 # 需求、技术方案、接口协议、同步算法、实现进度
└── data/                # 运行时数据(自动生成,gitignore)
```
