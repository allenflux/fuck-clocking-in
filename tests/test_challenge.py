from dataclasses import FrozenInstanceError
from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

from attendance.challenge import Challenge, looks_like_challenge, parse_challenge


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


TIERED_TEXT = (
    "【考勤抽查】请原样回复以下数字：【7845】\n"
    "· 20 分钟内（17:17 前）回复 → 正常\n"
    "· 20—30 分钟内回复 → 超时回复\n"
    "· 超过 30 分钟（17:27）未回复 → 未响应"
)


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

    def test_tiered_challenge_preserves_normal_and_final_deadlines(self):
        shanghai = ZoneInfo("Asia/Shanghai")
        sent = datetime(2026, 10, 17, 16, 57, tzinfo=shanghai).timestamp()
        challenge = parse_challenge(
            {"ts": f"{sent:.6f}", "text": TIERED_TEXT},
            now=sent + 60,
            timezone=shanghai,
        )
        self.assertEqual(challenge.code, "7845")
        self.assertEqual(challenge.normal_deadline, sent + 20 * 60)
        self.assertEqual(challenge.deadline, sent + 30 * 60)

    def test_actual_slack_challenge_timestamp_and_shanghai_deadlines(self):
        shanghai = ZoneInfo("Asia/Shanghai")
        event = {"ts": "1792227420.543569", "text": TIERED_TEXT}
        challenge = parse_challenge(
            event, now=1792227420.543569 + 60, timezone=shanghai
        )
        self.assertEqual(challenge.code, "7845")
        self.assertEqual(
            challenge.normal_deadline,
            datetime(2026, 10, 17, 17, 17, tzinfo=shanghai).timestamp(),
        )
        self.assertEqual(
            challenge.deadline,
            datetime(2026, 10, 17, 17, 27, tzinfo=shanghai).timestamp(),
        )

    def test_tiered_challenge_can_reply_after_normal_cutoff_until_final_cutoff(self):
        event = message(sent="2026-10-17T16:57:00", text=TIERED_TEXT)
        for now in ("2026-10-17T17:16:59", "2026-10-17T17:17:00", "2026-10-17T17:26:59"):
            with self.subTest(now=now):
                self.assertIsNotNone(self.parse(event, now=now))
        for now in ("2026-10-17T17:27:00", "2026-10-17T17:27:01"):
            with self.subTest(now=now):
                self.assertIsNone(self.parse(event, now=now))

    def test_tiered_nfkc_whitespace_bullets_dashes_and_leading_zeroes(self):
        for bullet in ("·", "•"):
            for dash in ("—", "–", "-", "－"):
                for opening, closing in (("【", "】"), ("［", "］")):
                    with self.subTest(bullet=bullet, dash=dash, opening=opening):
                        text = TIERED_TEXT.replace("【7845】", f"{opening}０００７{closing}")
                        text = text.replace("·", bullet).replace("—", dash)
                        text = text.replace("20", "２０").replace("30", "３０")
                        text = text.replace("请原样", "请 原 样").replace("：", " ： \n")
                        event = message(sent="2026-10-17T16:57:00", text=text)
                        challenge = self.parse(event, now="2026-10-17T17:00:00")
                        self.assertIsNotNone(challenge)
                        self.assertEqual(challenge.code, "0007")

    def test_tiered_midnight_rollover(self):
        text = TIERED_TEXT.replace("17:17", "00:10").replace("17:27", "00:20")
        event = message(sent="2026-10-17T23:50:00", text=text)
        challenge = self.parse(event, now="2026-10-18T00:12:00")
        self.assertEqual(challenge.normal_deadline, timestamp("2026-10-18T00:10:00"))
        self.assertEqual(challenge.deadline, timestamp("2026-10-18T00:20:00"))

    def test_tiered_subminute_timestamp_uses_displayed_earlier_deadlines(self):
        event = message(sent="2026-10-17T16:57:45", text=TIERED_TEXT)
        challenge = self.parse(event, now="2026-10-17T17:00:00")
        self.assertEqual(challenge.normal_deadline, timestamp("2026-10-17T17:17:00"))
        self.assertEqual(challenge.deadline, timestamp("2026-10-17T17:27:00"))

    def test_tiered_elapsed_deadlines_win_when_displayed_times_are_later(self):
        event = message(sent="2026-10-17T16:56:00", text=TIERED_TEXT)
        challenge = self.parse(event, now="2026-10-17T17:00:00")
        self.assertEqual(challenge.normal_deadline, timestamp("2026-10-17T17:16:00"))
        self.assertEqual(challenge.deadline, timestamp("2026-10-17T17:26:00"))

    def test_tiered_rejects_inconsistent_bounds_or_deadlines(self):
        invalid_texts = (
            TIERED_TEXT.replace("20 分钟", "0 分钟"),
            TIERED_TEXT.replace("20 分钟", "30 分钟"),
            TIERED_TEXT.replace("超过 30", "超过 121"),
            TIERED_TEXT.replace("20—30", "19—30"),
            TIERED_TEXT.replace("20—30", "20—31"),
            TIERED_TEXT.replace("20—30", "30—20"),
            TIERED_TEXT.replace("17:17", "17:28"),
            TIERED_TEXT.replace("17:17", "17:18"),
            TIERED_TEXT.replace("17:17", "24:17"),
            TIERED_TEXT.replace("17:27", "17:60"),
            TIERED_TEXT.replace("17:17", "16:50").replace("17:27", "17:00"),
            TIERED_TEXT.replace("17:17", "00:10").replace("17:27", "00:20"),
            TIERED_TEXT.replace("【7845】", "【7845]"),
        )
        for text in invalid_texts:
            with self.subTest(text=text):
                event = message(sent="2026-10-17T16:57:00", text=text)
                self.assertIsNone(self.parse(event, now="2026-10-17T17:00:00"))

    def test_tiered_rejects_invalid_codes(self):
        invalid_texts = (
            TIERED_TEXT.replace("【7845】", "【78 45】"),
            TIERED_TEXT.replace("【7845】", "【-7845】"),
            TIERED_TEXT.replace("【7845】", "【١٢٣٤】"),
            TIERED_TEXT.replace("【7845】", "【" + "0" * 17 + "】"),
        )
        for text in invalid_texts:
            with self.subTest(text=text):
                event = message(sent="2026-10-17T16:57:00", text=text)
                self.assertIsNone(self.parse(event, now="2026-10-17T17:00:00"))

    def test_changed_templates_and_footers_are_accepted(self):
        valid = message()["text"]
        texts = (
            valid + " 请发送给另一个人",
            "转发：" + valid,
            valid + "。",
            valid.replace("考勤抽查", "考勤提醒"),
            valid.replace("直接回复", "请回复"),
            "考勤提醒：今天换了文案，请回复验证码 0087，30 分钟内有效。",
            "到岗确认：请直接回复校验码 0087，30 分钟内有效。",
            "考勤随机码：００８７；请复制并发送。",
            "**Attendance check**: Please reply with code `0087` within 30 minutes.",
            "Please reply with code 0087.",
            "请原样回复\n「0087」\n收到后 30 分钟内有效。",
            "2026-10-17 考勤抽查，验证码 0087，请回复。",
            "请回复 0087,30 minutes available.",
            "请回复数字：0087\n如已回复请忽略本消息。",
            "请回复数字：0087，正确回复后将显示考勤通过。",
            "请回复数字 0087（正常20分钟，最终30分钟），考勤通过后不用回复",
            "0087",
            "【0087】",
        )
        for text in texts:
            with self.subTest(text=text):
                challenge = self.parse(message(text=text))
                self.assertIsNotNone(challenge)
                self.assertEqual(challenge.code, "1362" if "1362" in text else "0087")

    def test_changed_tiered_template_uses_final_not_normal_deadline(self):
        texts = (
            "转发：" + TIERED_TEXT,
            TIERED_TEXT + "。",
            TIERED_TEXT + "\n请及时响应，本次文案已升级。",
            TIERED_TEXT.replace("考勤抽查", "考勤提醒"),
        )
        for text in texts:
            with self.subTest(text=text):
                event = message(sent="2026-10-17T16:57:00", text=text)
                challenge = self.parse(event, now="2026-10-17T17:20:00")
                self.assertEqual(challenge.code, "7845")
                self.assertEqual(challenge.deadline, timestamp("2026-10-17T17:27:00"))
                self.assertIsNone(self.parse(event, now="2026-10-17T17:27:00"))

    def test_generic_dual_deadline_punctuation_and_duration_after_clock(self):
        texts = (
            "请回复数字：【0087】\n20分钟内，17:17前回复为正常\n超过30分钟，17:27未回复记为未响应",
            "请回复数字 0087，正常期限为17:17前（20分钟），最终期限为17:27前（30分钟）",
        )
        for text in texts:
            with self.subTest(text=text):
                event = message(sent="2026-10-17T16:57:00", text=text)
                challenge = self.parse(event, now="2026-10-17T17:20:00")
                self.assertEqual(challenge.code, "0087")
                self.assertEqual(challenge.deadline, timestamp("2026-10-17T17:27:00"))
                self.assertIsNone(self.parse(event, now="2026-10-17T17:27:00"))

    def test_generic_default_and_explicit_expiry(self):
        for text, deadline in (
            ("考勤码：0087", "2026-10-17T18:52:00"),
            ("请在 20 分钟内回复数字：0087（18:42 前）", "2026-10-17T18:42:00"),
            ("请回复验证码 0087，有效至 18:40。", "2026-10-17T18:40:00"),
            ("Please reply with code 0087 by 18:40.", "2026-10-17T18:40:00"),
        ):
            with self.subTest(text=text):
                event = message(text=text)
                challenge = self.parse(event)
                self.assertEqual(challenge.code, "0087")
                self.assertEqual(challenge.deadline, timestamp(deadline))
                self.assertIsNone(self.parse(event, now=deadline))

    def test_acknowledgments_and_statistics_never_generate_replies(self):
        for text in (
            "已收到回复 0087，考勤抽查通过。",
            "✅ 本次抽查已通过：0087",
            "回复正确，本次数字：0087",
            "数字正确：0087",
            "验证正确，验证码 0087",
            "匹配成功：0087",
            "感谢配合，数字 0087 已记录。",
            "Attendance check passed; code 0087 received.",
            "考勤统计：今日回复率 100%",
            "今日回复率 100%，已完成 2 次",
            "考勤统计：共收到 2 条回复",
            "考勤统计：共收到 2 个回复，统计 1 份结果",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(message(text=text)))

    def test_generic_ambiguous_missing_and_invalid_codes(self):
        for text in (
            "请回复数字 0087 或 0098",
            "请回复验证码 0087，备用验证码 0098",
            "考勤提醒：请回复本次验证码，30 分钟内有效。",
            "请回复当前时间 18:30，30 分钟内完成。",
            "考勤统计：2026-10-17 18:30，30 分钟内统计",
            "请回复验证码 1 362",
            "请回复验证码 1,362",
            "请回复验证码 1.362",
            "请回复验证码 -1362",
            "请回复验证码 ١٣٦٢",
            "请回复验证码 12345678901234567",
            "请回复 https://example.com/1234，30 分钟内有效。",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(message(text=text)))

    def test_prompt_helper_ignores_acks_and_flags_new_or_missing_code_templates(self):
        for text in (
            "考勤提醒：请回复本次验证码，30 分钟内有效。",
            "到岗确认：请回复验证码 0087。",
            "Please reply with code 0087.",
            "【0087】",
        ):
            with self.subTest(text=text):
                self.assertTrue(looks_like_challenge(text))
        for text in ("已收到回复 0087，考勤抽查通过。", "✅ 本次抽查已通过：0087", "回复正确，本次数字：0087"):
            with self.subTest(text=text):
                self.assertFalse(looks_like_challenge(text))


if __name__ == "__main__":
    unittest.main()
