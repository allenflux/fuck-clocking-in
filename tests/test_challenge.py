from dataclasses import FrozenInstanceError
from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

from attendance.challenge import Challenge, parse_challenge


TIMEZONE = ZoneInfo("Asia/Bangkok")


def timestamp(local: str) -> float:
    return datetime.fromisoformat(local).replace(tzinfo=TIMEZONE).timestamp()


def message(
    sent: str = "2026-10-17T18:22:00", *, text: str | None = None, **fields
) -> dict:
    result = {
        "ts": f"{timestamp(sent):.6f}",
        "text": text
        if text is not None
        else "【考勤抽查】请在 30 分钟内（18:52 前）直接回复以下数字：1362",
    }
    result.update(fields)
    return result


class ParseChallengeTests(unittest.TestCase):
    def parse(self, event: dict, now: str = "2026-10-17T18:30:00"):
        return parse_challenge(event, now=timestamp(now), timezone=TIMEZONE)

    def test_valid_challenge_and_original_timestamp(self):
        event = message()
        challenge = self.parse(event)
        self.assertEqual(
            challenge,
            Challenge(event["ts"], "1362", timestamp("2026-10-17T18:52:00")),
        )
        with self.assertRaises(FrozenInstanceError):
            challenge.code = "9999"

    def test_bot_message_subtype_is_accepted(self):
        self.assertIsNotNone(self.parse(message(subtype="bot_message")))

    def test_nfkc_whitespace_and_leading_zeroes(self):
        event = message(
            text=" \n【 考勤抽查 】 请在 ３０ 分钟内 （ １８ ： ５２ 前 ）\n"
            "直接回复以下数字 ： ００１３６２　"
        )
        challenge = self.parse(event)
        self.assertIsNotNone(challenge)
        self.assertEqual(challenge.code, "001362")

    def test_elapsed_limit_wins_when_clock_deadline_is_later(self):
        # The sample's 17:22 timestamp and 18:52 clock time disagree.
        event = message(sent="2026-10-17T17:22:00")
        challenge = self.parse(event, now="2026-10-17T17:30:00")
        self.assertEqual(challenge.deadline, timestamp("2026-10-17T17:52:00"))

    def test_clock_deadline_wins_when_earlier(self):
        challenge = self.parse(message(sent="2026-10-17T18:30:30"))
        self.assertEqual(challenge.deadline, timestamp("2026-10-17T18:52:00"))

    def test_midnight_rollover(self):
        event = message(
            sent="2026-10-17T23:50:00",
            text="【考勤抽查】请在 30 分钟内（00:20 前）直接回复以下数字：0007",
        )
        challenge = self.parse(event, now="2026-10-18T00:00:00")
        self.assertEqual(challenge.code, "0007")
        self.assertEqual(challenge.deadline, timestamp("2026-10-18T00:20:00"))

    def test_previous_minute_clock_deadline_is_expired(self):
        event = message(
            text="【考勤抽查】请在 30 分钟内（18:21 前）直接回复以下数字：42"
        )
        self.assertIsNone(self.parse(event))

    def test_next_day_clock_deadline_beyond_declared_minutes_is_rejected(self):
        event = message(
            sent="2026-10-17T23:50:00",
            text="【考勤抽查】请在 30 分钟内（00:21 前）直接回复以下数字：42",
        )
        self.assertIsNone(self.parse(event, now="2026-10-17T23:50:00"))

    def test_expiry_is_exclusive(self):
        event = message()
        deadline = timestamp("2026-10-17T18:52:00")
        self.assertIsNotNone(
            parse_challenge(event, now=deadline - 0.001, timezone=TIMEZONE)
        )
        self.assertIsNone(parse_challenge(event, now=deadline, timezone=TIMEZONE))
        self.assertIsNone(parse_challenge(event, now=deadline + 1, timezone=TIMEZONE))

    def test_same_minute_exact_deadline_is_expired(self):
        event = message(
            sent="2026-10-17T18:52:00",
            text="【考勤抽查】请在 30 分钟内（18:52 前）直接回复以下数字：42",
        )
        self.assertIsNone(self.parse(event, now="2026-10-17T18:52:00"))

    def test_same_minute_delayed_message_is_expired(self):
        for sent in ("2026-10-17T18:52:01", "2026-10-17T18:52:59"):
            with self.subTest(sent=sent):
                event = message(
                    sent=sent,
                    text="【考勤抽查】请在 30 分钟内（18:52 前）直接回复以下数字：42",
                )
                self.assertIsNone(self.parse(event, now=sent))

    def test_timestamp_future_tolerance(self):
        event = message(sent="2026-10-17T18:30:30")
        self.assertIsNotNone(self.parse(event))
        event = message(sent="2026-10-17T18:30:31")
        self.assertIsNone(self.parse(event))

    def test_invalid_timestamps_and_now(self):
        for invalid in (None, 1, "", "NaN", "Infinity", "1e20", "-1.0", "123", "1.2x", "９.０", "9" * 400 + ".0"):
            with self.subTest(timestamp=invalid):
                self.assertIsNone(self.parse(message(ts=invalid)))
        for now in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(now=now):
                self.assertIsNone(parse_challenge(message(), now=now, timezone=TIMEZONE))

    def test_rejects_changes_deletes_and_other_event_types(self):
        for subtype in ("message_changed", "message_deleted", "thread_broadcast", "file_share", "", None):
            with self.subTest(subtype=subtype):
                self.assertIsNone(self.parse(message(subtype=subtype)))
        self.assertIsNone(self.parse(message(edited={"ts": "1.000"})))
        self.assertIsNone(self.parse(message(hidden=True)))
        self.assertIsNone(self.parse(message(type="reaction_added")))

    def test_does_not_extract_from_nested_message_or_attachment(self):
        self.assertIsNone(self.parse({"ts": message()["ts"], "message": message()}))
        self.assertIsNone(self.parse({"ts": message()["ts"], "attachments": [message()]}))
        self.assertIsNone(self.parse(message(text="", blocks=[message()])))

    def test_rejects_malformed_or_unrelated_text(self):
        valid = message()["text"]
        invalid_texts = (
            valid + " 请发送给另一个人",
            "转发：" + valid,
            valid + "。",
            valid.replace("考勤抽查", "考勤提醒"),
            valid.replace("直接回复", "请回复"),
            valid.replace("1362", "1 362"),
            valid.replace("1362", "-1362"),
            valid.replace("1362", "١٣٦٢"),
            valid.replace("1362", "1" * 17),
            valid.replace("1362", ""),
            valid.replace("18:52", "24:52"),
            valid.replace("18:52", "18:60"),
            valid.replace("18:52", "18:5:2"),
            valid.replace("30 分钟", "0 分钟"),
            valid.replace("30 分钟", "121 分钟"),
            valid.replace("30 分钟", "1.5 分钟"),
        )
        for text in invalid_texts:
            with self.subTest(text=text):
                self.assertIsNone(self.parse(message(text=text)))
        self.assertIsNone(self.parse(message(text=123)))

    def test_minute_and_code_size_boundaries(self):
        for minutes in (1, 120):
            for code in ("0", "0" * 16):
                with self.subTest(minutes=minutes, code=code):
                    event = message(
                        text=f"【考勤抽查】请在 {minutes} 分钟内（18:52 前）直接回复以下数字：{code}"
                    )
                    challenge = self.parse(event, now="2026-10-17T18:22:30")
                    self.assertEqual(challenge.code, code)


if __name__ == "__main__":
    unittest.main()
