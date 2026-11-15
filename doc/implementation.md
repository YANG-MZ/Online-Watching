# 实现进度与模块实现说明(文档驱动)

| 项目名称 | movie-togher |
| --- | --- |
| 文档版本 | I1.0 |
| 更新日期 | 2026-11-14 |
| 需求依据 | doc/requirements.md(V1.1) |
| 设计依据 | doc/design.md(T1.0)、doc/api.md(A1.0)、doc/sync-protocol.md(S1.0) |

本文档按"文档驱动"方式记录实现过程:每个实现阶段先在 §2 阶段总表登记范围,完成后在对应小节记录实现说明、实现决策与测试结果,作为后续阶段的依据。

---

## 1. 实现流程约定

每个实现阶段遵循同一流程:

1. **登记**:在 §2 阶段总表登记本阶段范围(自底向上,依赖先行);
2. **实现**:编码,实现决策实时记录于对应小节;
3. **测试**:pytest 单元测试覆盖实现小节全部公共行为与边界;
4. **归档**:测试结果写入本文档,失败用例必须修复或说明原因,不得隐瞒。

## 2. 阶段总表(依赖层次自底向上)

| 阶段 | 范围(依赖方向) | 状态 |
| --- | --- | --- |
| P0 | 工程骨架:目录结构 / models 数据结构 / 抽象接口 / config / logger | ✅ 完成(2026-11-14) |
| **P1** | **最底层业务:playback 结算服务 + SQLite 仓储 + LocalVideoStorage** | ✅ **完成(2026-11-14,见 §3)** |
| **P2** | **room_manager 领域服务:生命周期 / 成员 / 移交 / 快照 / 复活** | ✅ **完成(2026-11-15,见 §4)** |
| **P3** | **适配层:api(HTTP 端点)/ ws(WS 端点与广播)/ cleanup(清理任务)+ main 装配** | ✅ **完成(2026-11-15,见 §5)** |
| P4 | 装配与集成:main 工厂接线(_make_* 槽位)+ TestClient 集成测试 | 待开始 |
| P5 | 前端 web/:Vite + React + TS(同步引擎按 sync-protocol §4) | 待开始 |

---

## 3. P1:最底层业务逻辑(本阶段)

### 3.1 PlaybackService(server/app/playback.py)

实现 sync-protocol.md §1 推算公式与 §2 结算规则,纯计算服务:

| 方法 | 规则 |
| --- | --- |
| `compute_position(pb, now)` | playing: `position + max(0, now - updated_at) × rate`;paused: `position` |
| `apply_play(room, now)` | 结算 → `state=playing`,version+1;重复 play 位置连续(幂等无害) |
| `apply_pause(room, now)` | 结算 → `state=paused`,version+1 |
| `apply_seek(room, p, now)` | `position = max(0, p)`,**state 不变**,version+1 |
| `apply_rate(room, r, now)` | **先按旧倍速结算**再写新 rate,version+1 |
| `reset_for_new_video(room, now)` | paused/0/1.0,version+1 **不归零**(D4:旧视频迟到事件仍可被过滤) |

**实现决策:**

- 时间倒流(now < updated_at)时 elapsed 按 0 处理,位置不倒退;
- 倍速范围校验不在本服务(由 ws 层按配置 `[playback] allowed_rate_*` 校验),保持纯计算可测;
- 所有方法直接改写 `room.playback` 并返回同一对象(事件循环内单线程,无拷贝必要)。

**测试**(`server/tests/test_playback.py`,11 项):推算公式三态、play 暂停起步/幂等、pause 结算、seek 三态(state 保持/暂停生效/负数钳 0)、rate 旧倍速结算连续性、重置、version 单调性。

### 3.2 SQLite 仓储(server/app/db.py)

抽象接口(RoomRepository / VideoRepository)+ 生产实现(SqliteRoomRepository / SqliteVideoRepository)+ 共享连接封装 SqliteDatabase:

- **SqliteDatabase**:`check_same_thread=False` + `threading.Lock`(线程池并发串行)+ WAL;建库即执行 schema(rooms/videos/idx_videos_room)并写 `PRAGMA user_version = 1`;
- 所有公共方法为 `async`,同步体经 `asyncio.to_thread` 下放(design.md 4.6);
- **file_path 存相对 data 目录的 posix 字符串**(design.md 第 5 节约定),读取时返回 `Path`;
- 写入用 `INSERT OR REPLACE`(room_id/video_id 生成端有去重,此处兜底不崩溃);删除全部幂等;
- `delete_all_*` 返回 rowcount,供启动清理统计。

**测试**(`server/tests/test_db.py`,8 项):schema/PRAGMA、房间增删幂等/全删计数/同 ID 覆盖、视频按房列出/全列出/全删计数/删除幂等/同 ID 覆盖。

### 3.3 LocalVideoStorage(server/app/video_storage.py)

抽象接口 VideoStorage + 生产实现 LocalVideoStorage:

| 能力 | 实现 |
| --- | --- |
| `write_upload` | 流式写 `tmp/{upload_id}.part`;累计字节数实时校验超限;每 `abort_check_interval_bytes`(默认 64 MiB)在工作线程调用 `abort_check` 回调,返回 True 即中止;结束前 flush + fsync;**任何异常(含生成器异常/超限/中止)一律清理临时文件再抛出** |
| `promote_upload` | 校验临时文件存在 → `os.replace` 同卷原子重命名 → 返回正式路径 |
| `abort_upload` | 删临时文件,幂等 |
| `open_range` | `[start, start+length)` 分块(1 MiB)异步读(`asyncio.to_thread`),超出 EOF 返回实际可读部分 |
| `delete` | 删正式文件,幂等 |
| `sweep_tmp` | 按 mtime 清理超龄 `*.part`,返回删除数 |

**实现决策:**

- **中止复查用回调注入**(`abort_check: Callable[[], bool]`):design.md 4.7 步骤 6 要求复查"请求发起者仍为房主且房间未解散",存储层不感知房间语义,由 api 层在上传时注入闭包——解耦存储与房间模型;
- fsync 在 `write_upload` 末尾(fd 尚未关闭时)完成,`promote_upload` 仅做原子重命名(design.md 4.7 步骤 7 的落地细化);
- 异常类型:`UploadSizeExceededError`(验收 11)/ `UploadAbortedError`,均继承 `VideoStorageError` 便于上层统一映射错误码。

**测试**(`server/tests/test_video_storage.py`,14 项):目录创建、写入+提升、超限、中止触发/不触发、abort 幂等、提升缺失临时文件报错、生成器异常清理、Range 三态(中段/超 EOF/从 0)、删除幂等、超龄清扫、空目录清扫。

### 3.4 P1 测试结果汇总

| 测试文件 | 用例数 | 结果 |
| --- | --- | --- |
| server/tests/test_playback.py | 11 | ✅ 全部通过 |
| server/tests/test_db.py | 8 | ✅ 全部通过 |
| server/tests/test_video_storage.py | 14 | ✅ 全部通过 |
| server/tests/test_skeleton.py(骨架回归) | 4 | ✅ 全部通过 |
| **合计** | **37** | **✅ 38 passed(含参数化折算)** |

> 过程记录:首轮运行 1 项失败(test_apply_play_from_paused_settles),经核查为**测试期望自身写错**(paused 起步应从暂停位置继续,不加 elapsed;实现符合 sync-protocol §1 公式),修正测试断言后全绿;实现代码无改动。

## 4. P2:RoomManager 领域服务(2026-11-15)

### 4.1 实现范围(server/app/room_manager.py + models.py 扩展)

**房间生命周期**:创建(昵称校验/上限拒绝,房间 ID 冲突重试)、加入(签发凭证,不占名额)、解散(仅房主,`_destroy_room` 按"文件 → DB 行 → 内存"顺序立即清理)、过期清理(`purge_expired_rooms`,供 cleanup 调用)。

**成员与在线**:WS `authenticate`(房间存在 → token → 满员按在线口径 → 踢旧 → 加入/接管 → 取消空房倒计时)、`resolve_member`/`touch`(控制消息鉴权与心跳刷新)、`sweep_timeouts`(超时移除,**房主掉线不移交**)、`leave_room`(幂等;房主退出按 joined_at 升序移交,广播 host_changed)。

**上传三段式**:`begin_upload`(房主校验 + 409 互斥锁)→ `upload_abort_check`(回调注入存储层,房间解散/房主变更返回 True)→ `complete_upload`(替换删旧文件旧行、写新行、状态重置、返回 video_ready/video_changed)/ `fail_upload`(释放锁,幂等)。

**快照**:`snapshot_payload` 按 api.md 3.2 结构构造(members 按 joined_at 升序,server_ts 由 ws 层补齐)。

### 4.2 实现决策

- **模型扩展(相对 design.md 4.2)**:`known_users` 由 `dict[str, float]` 扩展为 `KnownUser(user_id/nickname/joined_at)`,并新增 `credentials: dict[token, user_id]`——WS 鉴权需按 token 定位用户,重连需沿用昵称,这是实现揭示的最小必要扩展,业务规则不变;
- **事件机制**:RoomManager 不直接广播,所有变动以 `RoomEvent(room_id, message, kick_conn=None)` 列表返回;`kick_conn` 非空表示向该连接单发 kicked 并由 ws 层 close 4004(决策 T15),否则向房间全体广播;
- **错误类型**:8 个 `RoomError` 子类,`code`/`message` 与 api.md §4 错误码一一对应,api 层按 code 映射 HTTP 状态码即可;
- **upload_abort_check 为同步方法**:仅读内存状态(单事件循环串行变更),在工作线程中被存储层调用,无锁安全。

### 4.3 测试结果(server/tests/test_room_manager.py,25 项 ✅)

覆盖:创建(凭证/昵称校验/上限)、加入与鉴权(404/4001/4002 口径)、踢旧不重复、重连沿用、退出幂等、空房复活、移交最早成员/最后一人不移交、**掉线不移交重连恢复**(验收 14)、解散全清理(验收 15)、权限拒绝(验收 9)、过期清理含文件(验收 10)、上传三段(首传/替换/互斥/失败释放/abort_check)、快照结构与 resolve/touch。

**过程记录**:首轮 19 项失败,分类处置——
- 测试侧笔误:`t=` 应为 `now=`(参数名)、`begin_upload` 误 await、`b"new!!"` 实为 5 字节与元数据不符、心跳超时用例时间计算错(50s < 90s 阈值);
- **实现侧真 bug 1 处**:`_destroy_room` 对同步方法 `stat_size` 误用 `await`(测试暴露),已修复;
- 修复后 63 项全部通过(38 P0/P1 回归 + 25 P2)。

## 5. P3:适配层与装配(2026-11-15)

### 5.1 实现范围

- **ws.py**:`ConnectionRegistry`(注册/移除防误删/单发/广播/房间级关闭)、`apply_room_events` 消费领域事件(kick 单发 + close 4004)、WS 端点(auth 10s 超时 → 鉴权错误先回 error 消息再按 api.md 3.5 close → auth_ok 快照 → 广播 member_joined → 消息循环 ping/pong 与 play/pause/seek/rate 仅房主)、`heartbeat_pass` 单轮扫描(独立函数便于测试直驱)+ `heartbeat_scan` 周期协程;
- **api.py**:全部 7 个 HTTP 端点;上传按 design.md 4.7 十步组装(409 互斥暂存延后抛出以保持"房主→扩展名→大小→互斥"校验顺序);`_parse_range` 单段 Range(闭区间/开区间/后缀式,end 越界钳制);错误统一 `{code, message}`(RoomError 映射表 + ApiError);
- **cleanup.py**:启动清理(DB 行全部视为残留,先删文件再删行)+ 周期扫描(空房过期/临时文件超龄/孤儿文件兜底)+ start/stop 生命周期;
- **main.py**:依赖图装配 + lifespan(启动清理、周期清理任务、心跳扫描)+ CORS;`app = build_app()` 供 uvicorn 引用。

### 5.2 实现决策与接口变更

- **VideoStorage 新增 `list_video_ids()`**(P3 清理任务孤儿核对所需,最小接口扩展);
- **共享注册表注入**:`create_ws_router` 接受外部 `ConnectionRegistry`,装配层必须传入与 api 层相同实例——否则 HTTP 触发的 leave/dissolve/上传广播无法到达 WS 连接(见 5.3 真 bug);`create_api_router` 不再需要仓储参数,`create_media_router` 仅需 storage;
- **广播静默失败改为记录 warning 日志**(loguru),便于排查连接级异常;
- **心跳扫描拆分 `heartbeat_pass`**:单轮逻辑独立为模块级异步函数,周期协程仅负责 sleep 循环,测试可直接驱动单轮;
- 测试装置(conftest.py)组装真实组件 + 临时目录,应用不带 lifespan(周期任务不启动,保持确定性);lifespan 装配由 P4 集成测试覆盖。

### 5.3 测试结果与过程记录

**新增 35 项**(test_api 16 / test_ws 12 / test_cleanup 4 / heartbeat_pass 等),累计 **98 项全部通过,两次连续回归 REAL_EXIT=0**。

覆盖:HTTP 端点成功/错误码映射(400/401/403/404/409/413/429)、上传成功/替换/广播、Range 三态与 416、leave/dissolve 的 WS 关闭行为(1000/4005)、WS 鉴权与 close code(4001/4002/4003/4004)、控制消息广播与越权、非法消息、心跳扫描广播、清理任务三职责与生命周期。

**过程记录(如实)**:

1. **实现侧真 bug 1 处(挂起级)**:`create_ws_router` 内部自建 `ConnectionRegistry`,与 api 层注入实例不一致 → leave/dissolve/上传广播永远无法到达 WS 连接,相关用例无限挂起。经 faulthandler 抓栈 + 日志插桩定位,改为装配层注入共享实例(§5.2),此为跨适配层依赖关系错误;
2. 测试侧错误若干:starlette 1.10 的 `websocket_connect` 必须以 `with`/`enter_context` 使用(新版 Session 在 `__enter__` 才启动 portal);断言顺序错误——`member_joined` 广播在成员 WS auth 后才发出,`video_ready` 广播不补发(先于上线完成的上传,上线者从快照得知视频);若干 `t=`/`now=` 参数名笔误与字节数不一致;
3. 调试手段沉淀:pytest 挂起用 `faulthandler.dump_traceback_later(…, file=…)` 写文件抓主线程栈(转储会被 pytest capture 吞掉,必须显式指定文件);管道尾 `echo $?` 会掩盖真实退出码,需以文件重定向取 timeout 真实退出码。

## 6. 对后续阶段的输入(P4 依赖)

- main.build_app 已完成全链路装配(lifespan 启动清理 + 周期清理 + 心跳扫描),P4 重点为**集成测试**:带 lifespan 的 TestClient 验证"重启即清理"(需求 4.5)、配置覆盖(验收 17)、多房间并发上限(验收 16)等跨层场景;
- P3 测试装置(conftest.py 的 p3_env)不带 lifespan,可复用为集成测试底座并按需开启 lifespan;
- 前端 P5 按 design.md 第 6 节与 doc/sync-protocol.md §4 实现,后端接口契约以 doc/api.md 为准,服务端消息结构已在 test_api/test_ws 中实测锁定。
