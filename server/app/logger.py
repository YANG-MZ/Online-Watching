"""日志模块(选型 T11:loguru)。

- 统一输出到 stderr + data/logs/app.log(轮转);
- 通过 InterceptHandler 拦截 uvicorn/fastapi 的标准库日志,
  使全部日志走同一格式与目的地;
- 本模块为横切基础设施,不依赖任何业务模块。
"""

from __future__ import annotations

import inspect
import logging
import sys
from pathlib import Path

from loguru import logger

from server.app.config import LogSettings

_LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)


class InterceptHandler(logging.Handler):
    """把标准库 logging 记录转发给 loguru(uvicorn/starlette 内部日志)。"""

    def emit(self, record: logging.LogRecord) -> None:
        # 找到日志调用方的真实帧,保留正确的模块/行号
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging(settings: LogSettings, log_dir: Path | None = None) -> None:
    """初始化全局日志:stderr + 可选文件轮转,并接管标准库日志。

    Args:
        settings: 日志配置(级别)。
        log_dir: 日志目录;为 None 时不写文件(便于测试)。
    """
    level = settings.level.upper()

    logger.remove()
    logger.add(sys.stderr, level=level, format=_LOG_FORMAT)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            log_dir / "app.log",
            level=level,
            format=_LOG_FORMAT,
            rotation="10 MB",
            retention=7,
            enqueue=True,
        )

    # 接管标准库 logging(uvicorn/fastapi 的启动与访问日志)
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers = [InterceptHandler()]
        logging.getLogger(name).propagate = False
