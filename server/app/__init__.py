# app 包:FastAPI 应用各模块
# 依赖层次(自底向上,禁止反向依赖):
#   models(数据结构,零依赖)
#     ← security(纯工具) / playback(领域算法)
#     ← db / video_storage(抽象仓储与存储接口)
#     ← room_manager(领域服务,组合上述模块)
#     ← api / ws / cleanup(适配层)
#     ← main(装配)
#   config / logger 为横切基础设施,仅被上层引用,自身不依赖业务模块。
