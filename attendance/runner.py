from __future__ import annotations

import logging
import math
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .challenge import parse_challenge
from .config import Config, ConfigurationError
from .slack import SlackClient, SlackError
from .state import StateStore


LOG = logging.getLogger(__name__)


def timestamp(message: dict[str, Any]) -> float:
    try:
        value = float(message.get("ts", ""))
        return value if math.isfinite(value) and value >= 0 else -1
    except (TypeError, ValueError):
        return -1


def is_sender(message: dict[str, Any], sender: str) -> bool:
    key = "bot_id" if sender.startswith("B") else "user"
    return message.get(key) == sender


def own_reply(message: dict[str, Any], user: str, code: str) -> bool:
    return (message.get("user") == user and not message.get("bot_id")
            and message.get("subtype") in (None, "")
            and message.get("thread_ts") in (None, "", message.get("ts"))
            and isinstance(message.get("text"), str)
            and message["text"].strip() == code)


@contextmanager
def process_lock(path: Path):
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a", encoding="utf-8") as lock:
        path.chmod(0o600)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigurationError("该状态文件已有运行中的程序，请勿启动第二个实例") from None
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class Responder:
    def __init__(self, config: Config, client: SlackClient, store: StateStore,
                 user: str, stop: threading.Event):
        self.config, self.client, self.store = config, client, store
        self.user, self.stop = user, stop

    def poll(self, *, dry_run: bool = False) -> int:
        now = time.time()
        messages = self.client.history(self.config.channel, oldest=now - 7201)
        messages.sort(key=timestamp)
        challenges = []
        for message in messages:
            if not is_sender(message, self.config.sender):
                continue
            sent_at = timestamp(message)
            if (sent_at < 0 or sent_at > time.time() + 30
                    or message.get("thread_ts") not in (None, "", message.get("ts"))):
                continue
            # Parse expired requests too: they are boundaries for repeated codes.
            challenge = parse_challenge(message, now=sent_at, timezone=self.config.timezone)
            if challenge:
                challenges.append(challenge)
        count = 0
        for index, challenge in enumerate(challenges):
            if self.stop.is_set():
                break
            next_ts = float(challenges[index + 1].ts) if index + 1 < len(challenges) else math.inf
            reply = next((m for m in messages
                          if float(challenge.ts) < timestamp(m) < next_ts
                          and timestamp(m) < challenge.deadline
                          and own_reply(m, self.user, challenge.code)), None)
            status = self.store.get_status(self.config.channel, challenge.ts)
            if status in {"sent", "manual", "expired"}:
                continue
            if reply:
                if not dry_run:
                    self.store.mark(self.config.channel, challenge.ts, challenge.code, "manual", reply_ts=reply["ts"])
                LOG.info("已发现自己的回复，跳过抽查 ts=%s", challenge.ts)
                continue
            if time.time() >= challenge.deadline:
                if not dry_run:
                    self.store.mark(self.config.channel, challenge.ts, challenge.code, "expired")
                continue
            if status in {"pending", "uncertain"}:
                LOG.warning("发送结果待核实，避免重复发送 ts=%s；请检查私聊", challenge.ts)
                continue
            if dry_run:
                LOG.info("DRY RUN：将直接回复 %s，抽查 ts=%s", challenge.code, challenge.ts)
                count += 1
                continue
            self.store.mark(self.config.channel, challenge.ts, challenge.code, "pending")
            try:
                reply_ts = self.client.post_code(self.config.channel, challenge.code)
            except SlackError as exc:
                state = "uncertain" if exc.ambiguous else "retryable"
                self.store.mark(self.config.channel, challenge.ts, challenge.code, state)
                if exc.ambiguous:
                    LOG.error("发送结果不确定：%s；下次先核实历史，不盲目重发", exc)
                    continue
                if exc.retry_after is None:
                    exc.permanent = True
                raise
            self.store.mark(self.config.channel, challenge.ts, challenge.code, "sent", reply_ts=reply_ts)
            count += 1
            LOG.info("已回复抽查 ts=%s，回复 ts=%s", challenge.ts, reply_ts)
            if self.stop.wait(1.1):
                break
        if not dry_run:
            self.store.cleanup(time.time())
        return count

    def run(self, *, dry_run: bool = False) -> None:
        failures = 0
        LOG.info("开始监听私聊 %s，账号 %s，间隔 %s 秒，dry_run=%s",
                 self.config.channel, self.user, self.config.poll_seconds, dry_run)
        while not self.stop.is_set():
            delay = self.config.poll_seconds
            try:
                self.poll(dry_run=dry_run)
                failures = 0
            except InterruptedError:
                break
            except SlackError as exc:
                if exc.permanent:
                    raise
                failures += 1
                delay = max(self.config.poll_seconds, exc.retry_after or min(600, 30 * 2 ** min(failures, 5)))
                LOG.warning("Slack 请求失败：%s；%.0f 秒后重试", exc, delay)
            self.stop.wait(delay)


def verify_account(config: Config, client: SlackClient, *, live: bool) -> str:
    identity = client.call("auth.test")
    user = identity.get("user_id", "")
    if identity.get("bot_id") or not isinstance(user, str) or not re.fullmatch(r"[UW][A-Z0-9]+", user):
        raise ConfigurationError("Token 必须属于你的用户账号，不能使用 Bot Token")
    if config.expected_user and config.expected_user != user:
        raise ConfigurationError("Token 对应的用户与 SLACK_EXPECTED_USER_ID 不一致")
    if config.channel:
        conversation = client.call("conversations.info", channel=config.channel).get("channel", {})
        if not isinstance(conversation, dict) or conversation.get("is_im") is not True:
            raise ConfigurationError("目标必须是与考勤机器人的一对一私聊")
        if config.sender.startswith("U") and conversation.get("user") != config.sender:
            raise ConfigurationError("私聊对方与 SLACK_ATTENDANCE_SENDER_ID 不一致")
    if live and user == config.sender:
        raise ConfigurationError("考勤机器人不能配置为你自己的用户 ID")
    return user
