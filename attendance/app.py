from __future__ import annotations

import argparse
import json
import logging
import signal
import sqlite3
import threading
from pathlib import Path

from .config import Config, ConfigurationError, load_env
from .runner import Responder, process_lock, verify_account
from .slack import SlackClient, SlackError
from .state import StateStore


def main() -> int:
    parser = argparse.ArgumentParser(description="使用你自己的 Slack 账号自动回复指定考勤私聊")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--list-dms", action="store_true", help="只读：列出私聊 ID 和对方用户 ID")
    actions.add_argument("--inspect", action="store_true", help="只读：检查身份、目标及近期考勤格式")
    actions.add_argument("--once", action="store_true", help="检查一次并退出（会发送，除非加 --dry-run）")
    parser.add_argument("--dry-run", action="store_true", help="仅记录将发送的数字，不发送、不修改回复记录")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        load_env(args.env_file)
        config = Config.from_env()
        client = SlackClient(config.token, timeout=config.timeout_seconds, stop=stop)
        if not args.list_dms:
            config.require_target(live=not args.inspect)
        while not stop.is_set():
            try:
                user = verify_account(config, client, live=not (args.inspect or args.list_dms))
                break
            except SlackError as exc:
                if exc.permanent or args.inspect or args.list_dms or args.once:
                    raise
                delay = max(config.poll_seconds, exc.retry_after or 60)
                logging.warning("启动检查暂时失败：%s；%.0f 秒后重试", exc, delay)
                stop.wait(delay)
        else:
            return 0
        print(f"已验证 Slack 用户：{user}", flush=True)
        if args.list_dms:
            parameters = {"types": "im", "limit": 200, "exclude_archived": True}
            seen = set()
            while not stop.is_set():
                result = client.call("conversations.list", **parameters)
                for channel in result.get("channels", []):
                    print(f"私聊 {channel['id']}  对方用户 {channel.get('user', '?')}")
                cursor = result.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
                if cursor in seen:
                    raise SlackError("conversations.list", "invalid_cursor", permanent=True)
                seen.add(cursor)
                parameters["cursor"] = cursor
                if stop.wait(3):
                    break
            return 0
        if args.inspect:
            info = client.call("conversations.info", channel=config.channel)["channel"]
            print(f"私聊：{config.channel}  对方用户：{info.get('user', '?')}  时区：{config.timezone}")
            result = client.call("conversations.history", channel=config.channel, limit=15)
            for message in reversed(result.get("messages", [])):
                text = message.get("text", "")
                if isinstance(text, str) and "考勤抽查" in text:
                    preview = json.dumps(text[:240], ensure_ascii=False)
                    print(f"ts={message.get('ts')} user={message.get('user')} bot_id={message.get('bot_id')} text={preview}")
            print("只读检查完成，没有发送消息。")
            return 0
        with process_lock(Path(str(config.state_path) + ".lock")):
            with StateStore(config.state_path) as store:
                responder = Responder(config, client, store, user, stop)
                if args.once:
                    responder.poll(dry_run=args.dry_run)
                else:
                    responder.run(dry_run=args.dry_run)
        return 0
    except InterruptedError:
        return 0
    except (ConfigurationError, SlackError) as exc:
        logging.error("%s", exc)
        return 2
    except (OSError, ValueError, sqlite3.Error) as exc:
        logging.error("本地配置或状态错误（%s）；请检查文件权限和配置", type(exc).__name__)
        return 2
