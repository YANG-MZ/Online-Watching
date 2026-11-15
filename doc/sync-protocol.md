# 播放同步协议与算法

| 项目名称 | movie-togher |
| --- | --- |
| 文档版本 | S1.0 |
| 更新日期 | 2026-11-14 |
| 需求依据 | doc/requirements.md(V1.2)§3.4、§3.7、§4.1 |
| 配套文档 | doc/design.md、doc/api.md |

本文定义服务端权威状态的维护规则与客户端同步引擎的实现算法。目标(需求 4.1):

- 房主操作 → 其他成员端生效 **≤ 1 秒**;
- 稳态播放时各成员间偏差 **≤ 0.5 秒**。

---

## 1. 权威状态模型(需求 3.4.1)

服务端每房间维护:

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `state` | `playing \| paused` | 全局播放状态 |
| `position` | float 秒 | **`updated_at` 时刻**的播放位置 |
| `rate` | float | 播放倍速 |
| `updated_at` | float unix 秒 | 最近一次状态变更的服务端时刻 |
| `version` | int | 单调递增版本号 |

**推算公式**(服务端与客户端统一):

```
当前目标位置 target(t) =
    state == playing ? position + (t - updated_at) × rate
                     : position
其中 t 为服务端时钟的当前时刻(客户端使用 t ≈ 本地时钟 + clock_offset,见 §3)
```

客户端不得自行决定全局状态(需求 3.4.1);本地一切全局行为由 `state_update`/快照驱动。

---

## 2. 服务端结算规则

每个控制操作都在**处理时刻 `now`(服务端时钟)** 先结算旧位置、再写入新状态,`version` 一律 +1,随后广播 `state_update`:

| 操作(房主) | 结算与写入 | 说明 |
| --- | --- | --- |
| `play` | `position = target(now)`(若原为 playing 则不变),`state=playing`,`updated_at=now` | 幂等:重复 play 无害 |
| `pause` | `position = target(now)`,`state=paused`,`updated_at=now` | 暂停位置精确到处理时刻 |
| `seek(p)` | `position = max(0, p)`,**state 不变**,`updated_at=now` | 暂停态 seek 同样生效(需求 3.4.2);服务端不感知 duration,不设上界,客户端 clamp |
| `rate(r)` | `position = target(now)`,`rate=r`,`updated_at=now` | 先按旧倍速结算再换新倍速;`r ∈ [0.25, 3.0]`,超范围回 error |

**视频替换重置**(需求 D4):`state=paused, position=0, rate=1.0, version+1`,并广播 `video_changed` 与一条重置后的 `state_update`(在线成员本地状态同步归位)——`version` 延续递增(不归零),保证旧视频的迟到 `state_update` 仍能被 version 过滤。

**播放结束(ended)**:不产生任何服务端事件(需求 3.4.4)。`position` 可能随时间超过实际 duration,属预期;房主随后 `play` 或 `seek` 时正常结算。

---

## 3. 时钟偏差估计(需求 3.4.1:RTT 一半补偿)

利用心跳通道(`ping{t0}` / `pong{t0, server_ts}`,见 api.md §3.3):

```
一次样本:
  rtt    = t1 - t0                    (t1 = 收到 pong 的本地时刻)
  offset = server_ts + rtt/2 - t1     (假设上下行时延对称)

维护:
  保留最近 8 个样本,取 rtt 最小的 3 个样本中 offset 的中位数作为 clock_offset
  (rtt 小 → 网络抖动小 → 样本更可信;中位数抗异常值)
```

- 采样:心跳周期 30s 一次,重连成功后立即额外采样 1 次;
- 客户端估算服务端时刻:`server_now ≈ performance时钟/墙上时钟 + clock_offset`;
- 精度预期:局域网 RTT 通常 < 5ms,offset 误差 < 数毫秒,远小于 0.5s 目标;V1 不做更精细的时钟同步(需求 3.4.1);
- 快照(`auth_ok`)与 `state_update` 均携带 `server_ts`,消息处理时顺带刷新一次 offset 样本(单向,rtt 未知则不采)。

---

## 4. 客户端同步引擎

### 4.1 事件应用规则(收到 `state_update` / 快照)

```
apply(update):
  if update.version <= local.version: 丢弃           # 过期事件(需求 3.4.1)
  local = update
  switch update.state:
    playing → 立即执行一次 converge(见 4.2),期间 video.play()
    paused  → video.pause(); video.currentTime = update.position
```

- **快照(auth_ok)整体覆盖**本地 `version` 与状态(无条件,不比较版本);
- `video_changed` → 重设 `src` 加载新视频 → `version` 已随重置递增,按 paused/0 等待房主 play(验收 12);
- `play()` 被 autoplay 策略拒绝(需求 7.3):进入"待播放"状态,展示"点击画面开始播放"浮层;用户点击后调用 `video.play()` 并立即执行 `converge` 追赶。**期间不重试 play() 骚扰**,每 5s 重新检测一次 `video.paused` 状态。

### 4.2 周期校准(需求 3.4.3)

校准器每 `CALIB_INTERVAL = 5s` 运行一次(仅 `state == playing` 时):

```
converge():
  target = position + (server_now - updated_at) × rate     # server_now 含 clock_offset
  diff   = video.currentTime - target                       # >0 本地超前

  |diff| ≤ 0.3s  → 忽略(噪声区,避免频繁微调)
  0.3s < |diff| ≤ 2.0s
      → 渐进校准:video.playbackRate = rate × (1 - clamp(diff/4, -0.05, +0.05))
        # 超前→放慢最多 5%,落后→加快最多 5%;约 8~40s 内平滑收敛
        # 校准期间 UI 倍速显示仍显示全局 rate,不显示校准系数
        # diff 回到 ±0.1s 内 → playbackRate = rate(退出校准)
  |diff| > 2.0s
      → 硬校准:video.currentTime = target(直接 seek,需求 3.4.3"大偏差直接跳转")
```

**参数表**(均为客户端常量,实现期可调,不影响协议):

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `CALIB_INTERVAL` | 5s | 周期校准间隔(需求写 5~10s,取 5s) |
| `DEAD_ZONE` | 0.3s | 忽略阈值 |
| `SMOOTH_MAX` | 2.0s | 渐进/硬校准分界 |
| `RATE_ADJ_MAX` | 5% | 渐进校准最大速率偏移 |

设计依据:稳态偏差来源为各端解码/缓冲进度差与校准间隔内的漂移,量级远小于 0.3s 死区;0.5s 验收目标由"死区 0.3s + 局域网 RTT 毫秒级"保证。

### 4.3 房主端行为

- 房主 UI 操作(播放/暂停/松手 seek/倍速)**只发送 WS 消息,不本地直接生效**,与成员端同路径等待 `state_update` 回来后应用(设计选择:消除"房主先走、成员跟随"的双路径分叉,保证全员一致;局域网内回环延迟毫秒级,操作手感无感知差异);
- 房主端校准器与成员端完全一致(房主端同样可能因缓冲落后,需校准追赶)。

### 4.4 缓冲与慢网(需求 3.7)

- 本端 `waiting`(缓冲)事件:**不**上报服务端、不影响他人(需求:慢网只影响本人);
- 缓冲恢复(`playing` 事件)后,下一轮 `converge` 自动追赶(落后 2s 内渐进、超 2s 直接 seek);
- 因此各端可能短暂落后,但始终向同一 `target` 收敛,互不拖拽。

---

## 5. 边界情况汇总

| 场景 | 处理 |
| --- | --- |
| 视频自然播完(ended) | 各端自然结束,无全局事件(需求 3.4.4);校准器在 ended 后暂停运行;房主 `seek`/`play` 后恢复 |
| ended 后房主 play | `target = position + Δt×rate` 可能 > duration;客户端 clamp:若 target ≥ duration,则 `currentTime = 0` 后播放(与浏览器 ended→play 从头行为一致,全员同路径,保持一致) |
| duration 未知/不一致 | 服务端不感知 duration,position 单调推算;客户端一律 clamp 到 `[0, duration]` |
| 暂停态 seek | 服务端仅改 position/state 不变,客户端 `paused` 分支直接 `currentTime = position`(需求 3.4.2) |
| 视频替换 | version 延续递增 + `video_changed` 事件驱动重新加载(§4.1);旧视频迟到事件被 version 过滤 |
| 断线重连 | 快照整体覆盖后执行一次 `converge`(需求 3.7);重连期间本地继续播放,快照后收敛 |
| 服务重启(房间失效) | WS 4003 终态,前端提示"房间不存在或已关闭"(需求 4.5) |
| 倍速 2x 播放 | 推算公式按 rate 线性放大;漂移速度同倍放大,校准周期不变仍可控 |
| 房主未上线(掉线) | 服务端继续推算 target,成员端照常校准跟随(需求 3.1.5:不掉线移交) |
| 进度条拖动过程 | 仅 UI 更新,`change`(松手)才发 seek(需求 3.4.2,避免事件风暴) |

---

## 6. 端到端延迟预算(验收 8.2-1:≤ 1s)

| 环节 | 预算 |
| --- | --- |
| 房主 WS 上行(局域网) | ~10 ms |
| 服务端结算 + 广播 | ~5 ms(纯内存,事件循环内) |
| 成员端下行 | ~10 ms |
| 客户端应用(apply + play/seek) | ~50 ms |
| **合计** | **~100 ms** ≪ 1s(含 10 倍余量,覆盖 Wi-Fi 抖动) |

稳态偏差(验收 8.2-2:≤ 0.5s):死区 0.3s + 时钟/网络误差(毫秒级)+ 校准间隔内漂移(可忽略),目标可达;验证方式见 design.md 第 9 节验收映射。

---

## 7. 状态字段信任模型小结

| 数据 | 唯一权威 | 客户端权限 |
| --- | --- | --- |
| 全局播放状态/位置/倍速/版本 | 服务端内存 | 只读 + 推算 |
| 在线成员/房主 | 服务端内存 | 只读 |
| 视频元数据 | 服务端(SQLite + 内存) | 只读 |
| 音量/静音/全屏 | 各端本地 | 完全独立,不上报(需求 3.5) |
