"""配置管理模块(design.md 第 7 节、选型 T10)。

加载顺序:config.toml(内置 tomllib 解析) → 环境变量覆盖 → pydantic 校验。
- 配置文件路径默认为工作目录下 config.toml,可经环境变量
  MOVIE_TOGETHER_CONFIG 覆盖;
- 环境变量覆盖规则:MOVIE_TOGETHER__<节>__<键>(双下划线分层),
  值先尝试 JSON 解析(支持数字/布尔/列表),失败则按字符串处理。

本模块为横切基础设施,不依赖任何业务模块。
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

CONFIG_ENV_VAR = "MOVIE_TOGETHER_CONFIG"
ENV_PREFIX = "MOVIE_TOGETHER__"
DEFAULT_CONFIG_PATH = Path("config.toml")


class ServerSettings(BaseModel):
    """HTTP/WS 服务监听配置。"""

    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)


class FrontendSettings(BaseModel):
    """前端站点地址与单 URL 部署(design.md T18)。

    - base_url:用于拼接邀请链接;为空时返回相对路径由前端补全;
    - serve_dist:开启后由后端托管前端构建产物(单进程单端口,同源无
      CORS);dist_dir 为构建产物目录(相对工作目录)。
    """

    base_url: str = ""
    serve_dist: bool = False
    dist_dir: str = "web/dist"


class CorsSettings(BaseModel):
    """CORS 放行 origin 列表(前后端分离部署,决策 T16)。"""

    allowed_origins: list[str] = ["*"]


class LimitsSettings(BaseModel):
    """硬性容量上限(需求第 2 节,超限即拒绝)。"""

    max_rooms: int = Field(default=5, ge=1)
    max_members_per_room: int = Field(default=10, ge=1)
    max_upload_bytes: int = Field(default=4 * 1024**3, ge=1)


class RoomSettings(BaseModel):
    """房间生命周期配置(需求 3.1.7:全员离开后 N 秒过期)。"""

    empty_expire_seconds: float = Field(default=600.0, gt=0)


class HeartbeatSettings(BaseModel):
    """心跳配置(需求 3.6.2:30s ping / 90s 判死)。"""

    client_ping_seconds: float = Field(default=30.0, gt=0)
    server_timeout_seconds: float = Field(default=90.0, gt=0)
    scan_interval_seconds: float = Field(default=15.0, gt=0)

    @model_validator(mode="after")
    def _timeout_greater_than_ping(self) -> "HeartbeatSettings":
        if self.server_timeout_seconds <= self.client_ping_seconds:
            raise ValueError("server_timeout_seconds 必须大于 client_ping_seconds,否则正常心跳也会被判死")
        return self


class CleanupSettings(BaseModel):
    """后台清理任务配置(design.md 4.9)。"""

    scan_interval_seconds: float = Field(default=60.0, gt=0)
    temp_file_max_age_seconds: float = Field(default=3600.0, gt=0)


class StorageSettings(BaseModel):
    """数据目录:SQLite 与视频文件根目录(gitignore)。"""

    data_dir: str = "data"


class PlaybackSettings(BaseModel):
    """倍速服务端校验范围(sync-protocol.md §2)。"""

    allowed_rate_min: float = Field(default=0.25, gt=0)
    allowed_rate_max: float = Field(default=3.0, gt=0)

    @model_validator(mode="after")
    def _min_less_than_max(self) -> "PlaybackSettings":
        if self.allowed_rate_min >= self.allowed_rate_max:
            raise ValueError("allowed_rate_min 必须小于 allowed_rate_max")
        return self


class LogSettings(BaseModel):
    """日志级别(trace/debug/info/warning/error/critical)。"""

    level: str = "info"


class Settings(BaseModel):
    """全局配置根模型。"""

    server: ServerSettings = Field(default_factory=ServerSettings)
    frontend: FrontendSettings = Field(default_factory=FrontendSettings)
    cors: CorsSettings = Field(default_factory=CorsSettings)
    limits: LimitsSettings = Field(default_factory=LimitsSettings)
    room: RoomSettings = Field(default_factory=RoomSettings)
    heartbeat: HeartbeatSettings = Field(default_factory=HeartbeatSettings)
    cleanup: CleanupSettings = Field(default_factory=CleanupSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    playback: PlaybackSettings = Field(default_factory=PlaybackSettings)
    log: LogSettings = Field(default_factory=LogSettings)


def _parse_env_value(raw: str) -> object:
    """环境变量值类型转换:先尝试 JSON(数字/布尔/数组),失败按字符串。"""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def _merge_env_overrides(data: dict) -> dict:
    """将 MOVIE_TOGETHER__<节>__<键> 形式的环境变量合并进配置字典。"""
    for key, raw in os.environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        parts = key[len(ENV_PREFIX):].lower().split("__")
        if len(parts) != 2:
            continue
        section, option = parts
        data.setdefault(section, {})
        if isinstance(data[section], dict):
            data[section][option] = _parse_env_value(raw)
    return data


def load_settings(config_path: str | Path | None = None) -> Settings:
    """加载配置:toml 文件 → 环境变量覆盖 → pydantic 校验。

    配置文件不存在时使用全默认值(便于测试);校验失败抛出
    pydantic.ValidationError,由调用方决定终止启动。
    """
    path = Path(config_path or os.environ.get(CONFIG_ENV_VAR, "") or DEFAULT_CONFIG_PATH)
    data: dict = {}
    if path.is_file():
        with path.open("rb") as f:
            data = tomllib.load(f)
    return Settings.model_validate(_merge_env_overrides(data))
