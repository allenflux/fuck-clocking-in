from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigurationError(ValueError):
    pass


def load_env(path: Path) -> None:
    """Read literal KEY=VALUE pairs. Existing environment variables win."""
    if not path.exists():
        return
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ConfigurationError(f"配置文件第 {number} 行不是 KEY=VALUE")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ConfigurationError(f"配置文件第 {number} 行引号不匹配")
            value = value[1:-1]
        os.environ.setdefault(key, value)


def integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    channel: str
    sender: str
    expected_user: str
    timezone: ZoneInfo
    state_path: Path
    poll_seconds: int = 60
    timeout_seconds: int = 15

    @classmethod
    def from_env(cls) -> Config:
        token = os.environ.get("SLACK_USER_TOKEN", "").strip()
        if not token.startswith("xoxp-") or any(c.isspace() for c in token):
            raise ConfigurationError("请配置 SLACK_USER_TOKEN（你的 xoxp- 用户 OAuth Token）")
        channel = os.environ.get("SLACK_ATTENDANCE_CHANNEL_ID", "").strip()
        sender = os.environ.get("SLACK_ATTENDANCE_SENDER_ID", "").strip()
        expected = os.environ.get("SLACK_EXPECTED_USER_ID", "").strip()
        for name, value, pattern in (
            ("SLACK_ATTENDANCE_CHANNEL_ID", channel, r"D[A-Z0-9]+"),
            ("SLACK_ATTENDANCE_SENDER_ID", sender, r"[UB][A-Z0-9]+"),
            ("SLACK_EXPECTED_USER_ID", expected, r"[UW][A-Z0-9]+"),
        ):
            if value and not re.fullmatch(pattern, value):
                raise ConfigurationError(f"{name} 格式错误")
        try:
            timezone = ZoneInfo(os.environ.get("SLACK_TIMEZONE", "Asia/Bangkok"))
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigurationError("SLACK_TIMEZONE 必须是有效时区，如 Asia/Bangkok") from exc
        return cls(
            token=token, channel=channel, sender=sender, expected_user=expected,
            timezone=timezone,
            state_path=Path(os.environ.get("ATTENDANCE_STATE_PATH", "data/state.sqlite3")),
            poll_seconds=integer("POLL_INTERVAL_SECONDS", 60, 60, 600),
            timeout_seconds=integer("HTTP_TIMEOUT_SECONDS", 15, 5, 60),
        )

    def require_target(self, *, live: bool) -> None:
        if not self.channel:
            raise ConfigurationError("请先配置 SLACK_ATTENDANCE_CHANNEL_ID（D 开头的私聊 ID）")
        if live and (not self.sender or not self.expected_user):
            raise ConfigurationError("正式运行前请配置 SLACK_ATTENDANCE_SENDER_ID 和 SLACK_EXPECTED_USER_ID；可先运行 --inspect")
