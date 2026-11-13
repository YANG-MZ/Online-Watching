"""标识与凭证生成(design.md 4.10,需求 4.3)。

全部使用 secrets 模块密码学随机,保证不可预测;session_token 仅在
内存比对,不落库、不记日志。

本模块为纯工具层,零业务依赖(实现完整,无业务规则)。
"""

from __future__ import annotations

import secrets
import uuid

# Crockford Base32 字符集,去除易混淆字符 0/O/1/I(design.md 4.10)
ROOM_ID_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
ROOM_ID_LENGTH = 8
USER_ID_BYTES = 8  # 16 个十六进制字符
SESSION_TOKEN_BYTES = 32  # token_urlsafe(32) → 43 字符


def generate_room_id() -> str:
    """生成 8 位大写 Crockford Base32 房间 ID,如 `7QXK4M9R`。"""
    return "".join(secrets.choice(ROOM_ID_ALPHABET) for _ in range(ROOM_ID_LENGTH))


def generate_user_id() -> str:
    """生成 16 位十六进制用户 ID。"""
    return secrets.token_hex(USER_ID_BYTES)


def generate_session_token() -> str:
    """生成 session_token(token_urlsafe(32),43 字符)。"""
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def generate_video_id() -> str:
    """生成视频文件 ID(UUID4 hex,32 字符),即存储文件名主干。"""
    return uuid.uuid4().hex


def generate_upload_id() -> str:
    """生成上传会话 ID,用于 data/tmp/{upload_id}.part 临时文件。"""
    return uuid.uuid4().hex
