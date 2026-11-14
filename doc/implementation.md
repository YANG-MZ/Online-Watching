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
| P2 | room_manager 领域服务:生命周期 / 成员 / 移交 / 快照 / 复活 | 待开始 |
| P3 | 适配层:api(HTTP 端点)/ ws(WS 端点与广播)/ cleanup(清理任务) | 待开始 |
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

## 4. 对后续阶段的输入(P2 依赖)

- P2 `RoomManager` 可直接注入:`PlaybackService`(已实现)、`RoomRepository`/`VideoRepository`(已实现)、`VideoStorage`(已实现,含 `abort_check` 回调机制供 api 层上传时使用);
- P3 api 层上传流程按 design.md 4.7 组装:预检 → `write_upload(abort_check=闭包)` → `promote_upload` → DB 行 → `PlaybackService.reset_for_new_video` → 广播;
- P3 ws 层按 api.md §3 映射 close code 与错误码;`RoomError.code` 已与 api.md §4 错误码对齐。
