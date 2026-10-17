from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from attendance.config import Config, ConfigurationError
from attendance.runner import Responder, verify_account
from attendance.slack import SlackClient, SlackError
from attendance.state import StateStore


TIMEZONE = ZoneInfo("Asia/Bangkok")


def timestamp(local: str) -> float:
    return datetime.fromisoformat(local).replace(tzinfo=TIMEZONE).timestamp()


NOW = timestamp("2026-10-17T18:30:00")


def challenge(*, sent="2026-10-17T18:22:00", code="1362", deadline="18:52", **fields):
    result = {
        "ts": f"{timestamp(sent):.6f}",
        "type": "message",
        "subtype": "bot_message",
        "user": "UATTENDANCE",
        "bot_id": "BATTENDANCE",
        "text": f"【考勤抽查】请在 30 分钟内（{deadline} 前）直接回复以下数字：{code}",
    }
    result.update(fields)
    return result


def own_message(*, text="1362", sent="2026-10-17T18:25:00", **fields):
    result = {"ts": f"{timestamp(sent):.6f}", "user": "UOWNER", "text": text}
    result.update(fields)
    return result


def immediate_stop():
    stop = Mock(spec=threading.Event)
    stop.is_set.return_value = False
    stop.wait.return_value = False
    return stop


class ResponderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Config(
            token="xoxp-test",
            channel="DATTENDANCE",
            sender="UATTENDANCE",
            expected_user="UOWNER",
            timezone=TIMEZONE,
            state_path=Path(self.temp.name) / "state.sqlite3",
        )
        self.store = StateStore(self.config.state_path)
        self.addCleanup(lambda: self.store.close())
        self.stop = immediate_stop()
        self.events = [challenge()]
        self.client = Mock(spec=SlackClient)
        self.client.history.side_effect = lambda *args, **kwargs: deepcopy(self.events)
        self.client.post_code.return_value = f"{NOW:.6f}"
        self.clock = patch("attendance.runner.time.time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def responder(self, *, client=None, config=None):
        return Responder(config or self.config, client or self.client, self.store, "UOWNER", self.stop)

    def reopen_store(self):
        self.store.close()
        self.store = StateStore(self.config.state_path)

    def test_one_direct_reply_and_dedup_after_poll_and_restart(self):
        self.assertEqual(self.responder().poll(), 1)
        self.assertEqual(self.store.get_status(self.config.channel, self.events[0]["ts"]), "sent")
        self.assertEqual(self.responder().poll(), 0)
        self.reopen_store()
        self.assertEqual(self.responder().poll(), 0)
        self.client.post_code.assert_called_once_with("DATTENDANCE", "1362")
        self.stop.wait.assert_called_once_with(1.1)

    def test_existing_manual_reply_prevents_send(self):
        self.events.append(own_message(text=" 1362\n"))
        self.assertEqual(self.responder().poll(), 0)
        self.client.post_code.assert_not_called()
        self.assertEqual(self.store.get_status(self.config.channel, self.events[0]["ts"]), "manual")

    def test_user_oauth_reply_with_app_metadata_prevents_duplicate_send(self):
        # Slack user OAuth posts can retain the human user ID alongside bot/app IDs.
        self.events.append(own_message(
            bot_id="BUSEROAUTHAPP", app_id="AUSEROAUTHAPP",
            subtype=None, thread_ts=None,
        ))
        self.assertEqual(self.responder().poll(), 0)
        self.reopen_store()
        self.assertEqual(self.responder().poll(), 0)
        self.client.post_code.assert_not_called()
        self.assertEqual(self.store.get_status(self.config.channel, self.events[0]["ts"]), "manual")

    def test_app_reply_from_another_user_does_not_satisfy_challenge(self):
        self.events.append(own_message(
            user="UOTHERAPPUSER", bot_id="BUSEROAUTHAPP", app_id="AUSEROAUTHAPP",
            subtype=None, thread_ts=None,
        ))
        self.assertEqual(self.responder().poll(), 1)
        self.client.post_code.assert_called_once_with("DATTENDANCE", "1362")
        self.assertEqual(self.store.get_status(self.config.channel, self.events[0]["ts"]), "sent")

    def test_manual_reply_that_is_also_a_thread_parent_prevents_send(self):
        reply = own_message()
        reply["thread_ts"] = reply["ts"]
        self.events.append(reply)
        self.assertEqual(self.responder().poll(), 0)
        self.client.post_code.assert_not_called()
        self.assertEqual(self.store.get_status(self.config.channel, self.events[0]["ts"]), "manual")

    def test_other_users_and_thread_replies_do_not_count_as_manual_reply(self):
        self.events.extend([
            own_message(user="UOTHER"),
            own_message(sent="2026-10-17T18:26:00", thread_ts=self.events[0]["ts"]),
        ])
        self.assertEqual(self.responder().poll(), 1)
        self.client.post_code.assert_called_once_with("DATTENDANCE", "1362")

    def test_previous_manual_code_does_not_satisfy_a_repeated_challenge(self):
        self.events.extend([
            own_message(),
            challenge(sent="2026-10-17T18:27:00", deadline="18:57"),
        ])
        self.assertEqual(self.responder().poll(), 1)
        self.client.post_code.assert_called_once_with("DATTENDANCE", "1362")
        self.assertEqual(self.store.get_status(self.config.channel, self.events[0]["ts"]), "manual")
        self.assertEqual(self.store.get_status(self.config.channel, self.events[-1]["ts"]), "sent")

    def test_sender_user_id_filters_other_sources(self):
        self.events = [challenge(user="UOTHER")]
        self.assertEqual(self.responder().poll(), 0)
        self.client.post_code.assert_not_called()
        self.assertIsNone(self.store.get_status(self.config.channel, self.events[0]["ts"]))

    def test_challenge_that_is_also_a_thread_parent_is_answered(self):
        self.events[0]["thread_ts"] = self.events[0]["ts"]
        self.assertEqual(self.responder().poll(), 1)
        self.client.post_code.assert_called_once_with("DATTENDANCE", "1362")

    def test_challenge_inside_a_thread_is_not_answered(self):
        self.events[0]["thread_ts"] = f"{timestamp('2026-10-17T18:20:00'):.6f}"
        self.assertEqual(self.responder().poll(), 0)
        self.client.post_code.assert_not_called()
        self.assertIsNone(self.store.get_status(self.config.channel, self.events[0]["ts"]))

    def test_sender_bot_id_filters_other_bots(self):
        self.events = [challenge(bot_id="BOTHER")]
        config = replace(self.config, sender="BATTENDANCE")
        self.assertEqual(self.responder(config=config).poll(), 0)
        self.client.post_code.assert_not_called()
        self.events.append(challenge(sent="2026-10-17T18:23:00", code="0007"))
        self.assertEqual(self.responder(config=config).poll(), 1)
        self.client.post_code.assert_called_once_with("DATTENDANCE", "0007")

    def test_expired_challenge_is_recorded_without_sending(self):
        self.events = [challenge(sent="2026-10-17T18:00:00", deadline="18:30")]
        responder = self.responder()
        with self.assertLogs("attendance.runner", level="WARNING") as logs:
            self.assertEqual(responder.poll(), 0)
        self.assertIn("超过最终回复期限", logs.output[0])
        with self.assertNoLogs("attendance.runner", level="WARNING"):
            self.assertEqual(responder.poll(), 0)
        self.client.post_code.assert_not_called()
        self.assertEqual(self.store.get_status(self.config.channel, self.events[0]["ts"]), "expired")

    def test_multiline_challenge_replies_with_only_the_digits(self):
        self.events = [challenge(text=(
            "【考勤抽查】请原样回复以下数字：【7845】\n"
            "· 20 分钟内（18:42 前）回复 → 正常\n"
            "· 20—30 分钟内回复 → 超时回复\n"
            "· 超过 30 分钟（18:52）未回复 → 未响应"
        ))]
        self.assertEqual(self.responder().poll(), 1)
        self.client.post_code.assert_called_once_with("DATTENDANCE", "7845")

    def test_unknown_attendance_format_warns_once_without_message_body(self):
        self.events = [challenge(text="考勤提醒：请回复本次验证码，30 分钟内有效。秘密文本")]
        responder = self.responder()
        with self.assertLogs("attendance.runner", level="WARNING") as logs:
            self.assertEqual(responder.poll(), 0)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("消息格式未识别", logs.output[0])
        self.assertIn(self.events[0]["ts"], logs.output[0])
        self.assertNotIn("秘密文本", logs.output[0])
        with self.assertNoLogs("attendance.runner", level="WARNING"):
            self.assertEqual(responder.poll(), 0)
        self.events.append(challenge(sent="2026-10-17T18:23:00", text="到岗确认：请回复本次验证码，30 分钟内有效。"))
        with self.assertLogs("attendance.runner", level="WARNING") as new_logs:
            self.assertEqual(responder.poll(), 0)
        self.assertEqual(len(new_logs.output), 1)
        self.assertIn(self.events[-1]["ts"], new_logs.output[0])
        self.client.post_code.assert_not_called()

    def test_changed_wording_extracts_random_code_only_from_configured_sender(self):
        new_text = "考勤提醒：今天换了文案，请回复验证码 0087，30 分钟内有效。"
        self.events = [challenge(text=new_text, user="UOTHER")]
        responder = self.responder()
        self.assertEqual(responder.poll(), 0)
        self.events.append(challenge(text=new_text, sent="2026-10-17T18:23:00"))
        self.assertEqual(responder.poll(), 1)
        self.assertEqual(responder.poll(), 0)
        self.client.post_code.assert_called_once_with("DATTENDANCE", "0087")

    def test_acknowledgment_with_numeric_code_does_not_trigger_another_reply(self):
        self.events = [challenge(text="考勤提醒：请回复验证码 0087，30 分钟内有效。")]
        responder = self.responder()
        self.assertEqual(responder.poll(), 1)
        self.events.append(challenge(
            sent="2026-10-17T18:26:00", text="已收到回复 0087，考勤抽查通过。"
        ))
        with self.assertNoLogs("attendance.runner", level="WARNING"):
            self.assertEqual(responder.poll(), 0)
        self.assertIsNone(self.store.get_status(self.config.channel, self.events[-1]["ts"]))
        self.client.post_code.assert_called_once_with("DATTENDANCE", "0087")

    def test_existing_manual_reply_to_changed_wording_prevents_send(self):
        self.events = [
            challenge(text="到岗确认：请直接回复校验码 0087，30 分钟内有效。"),
            own_message(text="0087"),
        ]
        responder = self.responder()
        self.assertEqual(responder.poll(), 0)
        self.assertEqual(responder.poll(), 0)
        self.client.post_code.assert_not_called()
        self.assertEqual(self.store.get_status(self.config.channel, self.events[0]["ts"]), "manual")

    def test_successful_check_heartbeat_is_logged_first_then_every_fifteen_minutes(self):
        self.events = []
        responder = self.responder()
        with patch("attendance.runner.time.monotonic", return_value=100):
            with self.assertLogs("attendance.runner", level="INFO") as first:
                self.assertEqual(responder.poll(), 0)
        self.assertIn("检查完成：读取 0 条消息，回复 0 条", first.output[0])
        self.assertIn("2026-10-17T18:30:00+07:00", first.output[0])
        with patch("attendance.runner.time.monotonic", return_value=999):
            with self.assertNoLogs("attendance.runner", level="INFO"):
                responder.poll()
        with patch("attendance.runner.time.monotonic", return_value=1000):
            with self.assertLogs("attendance.runner", level="INFO") as later:
                responder.poll()
        self.assertIn("检查完成", later.output[0])

    def test_failed_check_does_not_log_successful_heartbeat(self):
        self.client.history.side_effect = SlackError("conversations.history", "ratelimited", retry_after=60)
        with self.assertNoLogs("attendance.runner", level="INFO"):
            with self.assertRaises(SlackError):
                self.responder().poll()

    def test_late_multiline_challenge_warns_and_still_replies_before_final_deadline(self):
        self.events = [challenge(text=(
            "【考勤抽查】请原样回复以下数字：【7845】\n"
            "· 20 分钟内（18:42 前）回复 → 正常\n"
            "· 20—30 分钟内回复 → 超时回复\n"
            "· 超过 30 分钟（18:52）未回复 → 未响应"
        ))]
        with patch("attendance.runner.time.time", return_value=timestamp("2026-10-17T18:45:00")):
            with self.assertLogs("attendance.runner", level="WARNING") as logs:
                self.assertEqual(self.responder().poll(), 1)
        self.assertIn("超过正常回复期限", logs.output[0])
        self.client.post_code.assert_called_once_with("DATTENDANCE", "7845")

    def test_challenge_beyond_future_tolerance_is_not_sent(self):
        self.events = [challenge(sent="2026-10-17T18:30:31", deadline="19:00")]
        self.assertEqual(self.responder().poll(), 0)
        self.client.post_code.assert_not_called()
        self.assertIsNone(self.store.get_status(self.config.channel, self.events[0]["ts"]))

    def test_leading_zeroes_are_sent_verbatim(self):
        self.events = [challenge(code="00001362")]
        self.assertEqual(self.responder().poll(), 1)
        self.client.post_code.assert_called_once_with("DATTENDANCE", "00001362")

    def test_dry_run_does_not_send_or_modify_state(self):
        self.assertEqual(self.responder().poll(dry_run=True), 1)
        self.client.post_code.assert_not_called()
        self.assertIsNone(self.store.get_status(self.config.channel, self.events[0]["ts"]))
        self.events.append(own_message())
        self.assertEqual(self.responder().poll(dry_run=True), 0)
        self.assertIsNone(self.store.get_status(self.config.channel, self.events[0]["ts"]))

    def test_crash_during_send_leaves_durable_pending_and_prevents_resend(self):
        def crash_after_pending(*args):
            self.assertEqual(self.store.get_status(self.config.channel, self.events[0]["ts"]), "pending")
            raise RuntimeError("simulated process crash")

        self.client.post_code.side_effect = crash_after_pending
        with self.assertRaisesRegex(RuntimeError, "simulated process crash"):
            self.responder().poll()
        self.reopen_store()
        self.assertEqual(self.responder().poll(), 0)
        self.client.post_code.assert_called_once()
        self.assertEqual(self.store.get_status(self.config.channel, self.events[0]["ts"]), "pending")

    def test_ambiguous_send_does_not_retry_and_can_be_reconciled(self):
        self.client.post_code.side_effect = SlackError("chat.postMessage", "transport_error", ambiguous=True)
        self.assertEqual(self.responder().poll(), 0)
        self.assertEqual(self.store.get_status(self.config.channel, self.events[0]["ts"]), "uncertain")
        self.reopen_store()
        self.assertEqual(self.responder().poll(), 0)
        self.client.post_code.assert_called_once()
        self.events.append(own_message(sent="2026-10-17T18:30:01"))
        self.assertEqual(self.responder().poll(), 0)
        self.assertEqual(self.store.get_status(self.config.channel, self.events[0]["ts"]), "manual")
        self.client.post_code.assert_called_once()

    def test_ambiguous_user_oauth_send_with_app_metadata_reconciles_after_restart(self):
        self.client.post_code.side_effect = SlackError("chat.postMessage", "transport_error", ambiguous=True)
        self.assertEqual(self.responder().poll(), 0)
        self.assertEqual(self.store.get_status(self.config.channel, self.events[0]["ts"]), "uncertain")
        self.reopen_store()
        self.events.append(own_message(
            sent="2026-10-17T18:30:01", bot_id="BUSEROAUTHAPP", app_id="AUSEROAUTHAPP",
            subtype=None, thread_ts=None,
        ))
        self.assertEqual(self.responder().poll(), 0)
        self.assertEqual(self.store.get_status(self.config.channel, self.events[0]["ts"]), "manual")
        self.client.post_code.assert_called_once()

    def test_rate_limit_is_retryable_and_next_poll_can_send(self):
        error = SlackError("chat.postMessage", "ratelimited", retry_after=60)
        self.client.post_code.side_effect = [error, f"{NOW:.6f}"]
        with self.assertRaises(SlackError) as caught:
            self.responder().poll()
        self.assertIs(caught.exception, error)
        self.assertFalse(error.permanent)
        self.assertEqual(self.store.get_status(self.config.channel, self.events[0]["ts"]), "retryable")
        self.assertEqual(self.responder().poll(), 1)
        self.assertEqual(self.store.get_status(self.config.channel, self.events[0]["ts"]), "sent")
        self.assertEqual(self.client.post_code.call_count, 2)

    def test_incomplete_paginated_history_prevents_any_send(self):
        client = SlackClient("xoxp-test", stop=self.stop, page_interval=0)
        client.call = Mock(side_effect=[
            {"ok": True, "messages": deepcopy(self.events),
             "response_metadata": {"next_cursor": "page-two"}, "has_more": True},
            SlackError("conversations.history", "ratelimited", retry_after=60),
        ])
        client.post_code = Mock()
        with self.assertRaises(SlackError):
            self.responder(client=client).poll()
        self.assertEqual(client.call.call_count, 2)
        self.assertEqual(client.call.call_args.kwargs["cursor"], "page-two")
        client.post_code.assert_not_called()
        self.assertIsNone(self.store.get_status(self.config.channel, self.events[0]["ts"]))


class VerifyAccountTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(
            token="xoxp-test", channel="DATTENDANCE", sender="UATTENDANCE",
            expected_user="UOWNER", timezone=TIMEZONE, state_path=Path("unused.sqlite3"),
        )

    def client(self, *, identity=None, conversation=None):
        client = Mock(spec=SlackClient)
        client.call.side_effect = [
            identity if identity is not None else {"ok": True, "user_id": "UOWNER"},
            {"ok": True, "channel": conversation if conversation is not None else {
                "id": "DATTENDANCE", "is_im": True, "user": "UATTENDANCE",
            }},
        ]
        return client

    def test_expected_user_and_private_conversation_are_verified(self):
        client = self.client()
        self.assertEqual(verify_account(self.config, client, live=True), "UOWNER")
        self.assertEqual(client.call.call_args_list[0].args, ("auth.test",))
        self.assertEqual(client.call.call_args_list[1].kwargs, {"channel": "DATTENDANCE"})

    def test_wrong_identity_is_rejected_before_conversation_lookup(self):
        client = self.client(identity={"ok": True, "user_id": "UOTHER"})
        with self.assertRaises(ConfigurationError):
            verify_account(self.config, client, live=True)
        client.call.assert_called_once_with("auth.test")

    def test_bot_identity_is_rejected(self):
        client = self.client(identity={"ok": True, "user_id": "UOWNER", "bot_id": "BBOT"})
        with self.assertRaises(ConfigurationError):
            verify_account(self.config, client, live=True)
        client.call.assert_called_once_with("auth.test")

    def test_public_channel_or_wrong_dm_peer_is_rejected(self):
        conversations = (
            {"id": "DATTENDANCE", "is_im": False, "user": "UATTENDANCE"},
            {"id": "DATTENDANCE", "is_mpim": True, "user": "UATTENDANCE"},
            {"id": "DATTENDANCE", "is_im": True, "user": "UOTHER"},
        )
        for conversation in conversations:
            with self.subTest(conversation=conversation):
                with self.assertRaises(ConfigurationError):
                    verify_account(self.config, self.client(conversation=conversation), live=True)

    def test_live_mode_rejects_self_as_attendance_sender(self):
        config = replace(self.config, sender="UOWNER")
        client = self.client(conversation={"is_im": True, "user": "UOWNER"})
        with self.assertRaises(ConfigurationError):
            verify_account(config, client, live=True)


if __name__ == "__main__":
    unittest.main()
