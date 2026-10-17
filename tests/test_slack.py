from email.message import Message
import io
import json
import threading
import unittest
import urllib.error
import urllib.parse
from unittest.mock import MagicMock, Mock, call, patch

from attendance.slack import SlackClient, SlackError, retry_seconds


def response(payload: object, *, raw: bytes | None = None) -> MagicMock:
    result = MagicMock()
    result.__enter__.return_value = result
    result.read.return_value = raw if raw is not None else json.dumps(payload).encode()
    return result


def form_parameters(request) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(request.data.decode("utf-8")))


class SlackClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = SlackClient("xoxp-test-secret", timeout=7, page_interval=0)
        self.opener = Mock()
        self.client._opener = self.opener

    def test_posts_exact_numeric_text_with_user_authorization(self) -> None:
        self.opener.open.return_value = response(
            {"ok": True, "channel": "D123", "ts": "100.002"}
        )
        self.assertEqual(self.client.post_code("D123", "0012"), "100.002")
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://slack.com/api/chat.postMessage")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer xoxp-test-secret")
        self.assertEqual(request.get_header("Content-type"), "application/json; charset=utf-8")
        self.assertEqual(self.opener.open.call_args.kwargs, {"timeout": 7})
        self.assertEqual(
            json.loads(request.data),
            {"channel": "D123", "text": "0012", "mrkdwn": False, "parse": "none",
             "unfurl_links": False, "unfurl_media": False},
        )
        self.assertNotIn("thread_ts", json.loads(request.data))
        self.assertNotIn("as_user", json.loads(request.data))

    def test_conversation_reads_encode_parameters_as_form_data(self) -> None:
        for method, parameters, expected in (
            ("conversations.info", {"channel": "D123", "include_locale": False},
             {"channel": "D123", "include_locale": "false"}),
            ("conversations.history", {"channel": "D123", "oldest": "123.000001",
                                       "inclusive": True, "limit": 15, "cursor": "next+/="},
             {"channel": "D123", "oldest": "123.000001", "inclusive": "true",
              "limit": "15", "cursor": "next+/="}),
            ("conversations.list", {"types": "im", "exclude_archived": True,
                                    "limit": 200, "cursor": "next+/="},
             {"types": "im", "exclude_archived": "true", "limit": "200",
              "cursor": "next+/="}),
        ):
            with self.subTest(method=method):
                self.opener.open.return_value = response({"ok": True})
                self.client.call(method, **parameters)
                request = self.opener.open.call_args.args[0]
                self.assertEqual(request.full_url, f"https://slack.com/api/{method}")
                self.assertEqual(request.get_method(), "POST")
                self.assertEqual(request.get_header("Authorization"), "Bearer xoxp-test-secret")
                self.assertEqual(request.get_header("Content-type"),
                                 "application/x-www-form-urlencoded; charset=utf-8")
                self.assertEqual(form_parameters(request), expected)
                self.assertNotIn("xoxp-test-secret", request.data.decode("utf-8"))

    def test_http_429_is_definitely_unaccepted_and_obeys_retry_after(self) -> None:
        headers = Message()
        headers["Retry-After"] = "123"
        self.opener.open.side_effect = urllib.error.HTTPError(
            "https://slack.com/api/chat.postMessage", 429, "limited", headers, io.BytesIO()
        )
        with self.assertRaises(SlackError) as caught:
            self.client.post_code("D123", "1362")
        self.assertEqual(caught.exception.code, "ratelimited")
        self.assertEqual(caught.exception.retry_after, 123)
        self.assertFalse(caught.exception.ambiguous)
        self.assertFalse(caught.exception.permanent)

    def test_transport_failures_are_ambiguous_only_for_writes(self) -> None:
        for problem in (TimeoutError(), OSError(), urllib.error.URLError("failed")):
            for method, ambiguous in (("chat.postMessage", True), ("auth.test", False)):
                with self.subTest(problem=type(problem).__name__, method=method):
                    self.opener.open.side_effect = problem
                    with self.assertRaises(SlackError) as caught:
                        self.client.call(method)
                    self.assertEqual(caught.exception.code, "transport_error")
                    self.assertEqual(caught.exception.ambiguous, ambiguous)

    def test_malformed_write_responses_remain_ambiguous(self) -> None:
        for raw in (
            b"not JSON", b"\xff", b"[]", b"null", b"{}", b"x" * 2_000_001
        ):
            with self.subTest(raw=raw[:15]):
                self.opener.open.return_value = response(None, raw=raw)
                with self.assertRaises(SlackError) as caught:
                    self.client.call("chat.postMessage")
                self.assertEqual(caught.exception.code, "invalid_response")
                self.assertTrue(caught.exception.ambiguous)

    def test_known_api_rejections_are_classified(self) -> None:
        for code, permanent, ambiguous, retry_after in (
            ("invalid_auth", True, False, None),
            ("missing_scope", True, False, None),
            ("channel_not_found", True, False, None),
            ("not_allowed_token_type", True, False, None),
            ("ratelimited", False, False, 60),
            ("rate_limited", False, False, 60),
            ("internal_error", False, True, None),
            ("fatal_error", False, True, None),
            ("request_timeout", False, True, None),
            ("service_unavailable", False, True, None),
        ):
            with self.subTest(code=code):
                self.opener.open.return_value = response({"ok": False, "error": code})
                with self.assertRaises(SlackError) as caught:
                    self.client.call("chat.postMessage")
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.permanent, permanent)
                self.assertEqual(caught.exception.ambiguous, ambiguous)
                self.assertEqual(caught.exception.retry_after, retry_after)

    def test_server_error_write_is_ambiguous(self) -> None:
        self.opener.open.side_effect = urllib.error.HTTPError(
            "https://slack.com/api/chat.postMessage", 503, "unavailable", Message(), io.BytesIO()
        )
        with self.assertRaises(SlackError) as caught:
            self.client.post_code("D123", "1362")
        self.assertEqual(caught.exception.code, "http_503")
        self.assertTrue(caught.exception.ambiguous)
        self.assertFalse(caught.exception.permanent)

    def test_untrusted_error_text_is_not_exposed(self) -> None:
        self.opener.open.return_value = response(
            {"ok": False, "error": "xoxp-test-secret\nmalicious detail"}
        )
        with self.assertRaises(SlackError) as caught:
            self.client.call("auth.test")
        self.assertEqual(caught.exception.code, "unknown_error")
        self.assertNotIn("secret", str(caught.exception))

    def test_post_requires_matching_channel_and_reply_timestamp(self) -> None:
        for payload in (
            {"ok": True, "channel": "D456", "ts": "1.002"},
            {"ok": True, "channel": "D123"},
            {"ok": True, "channel": "D123", "ts": 1.002},
        ):
            with self.subTest(payload=payload):
                self.opener.open.return_value = response(payload)
                with self.assertRaises(SlackError) as caught:
                    self.client.post_code("D123", "1362")
                self.assertTrue(caught.exception.ambiguous)

    def test_history_reads_all_pages_and_deduplicates_message_timestamps(self) -> None:
        self.opener.open.side_effect = [
            response({"ok": True, "messages": [{"ts": "3.001", "text": "new"},
                                                {"ts": "2.001", "text": "middle"}],
                      "has_more": True, "response_metadata": {"next_cursor": "page2"}}),
            response({"ok": True, "messages": [{"ts": "2.001", "text": "middle"},
                                                {"ts": "1.001", "text": "old"}],
                      "has_more": False, "response_metadata": {"next_cursor": ""}}),
            response({"ok": True, "messages": [{"ts": "3.001", "text": "new"}],
                      "has_more": False, "response_metadata": {"next_cursor": ""}}),
        ]
        messages = self.client.history("D123", oldest=0.5)
        self.assertEqual([item["ts"] for item in messages], ["3.001", "2.001", "1.001"])
        requests = [form_parameters(call.args[0]) for call in self.opener.open.call_args_list]
        self.assertEqual(requests[0], {"channel": "D123", "oldest": "0.500000", "limit": "15"})
        self.assertEqual(requests[1]["cursor"], "page2")
        self.assertEqual(requests[2], {"channel": "D123", "oldest": "3.001",
                                       "inclusive": "true", "limit": "15"})

    def test_history_refresh_merges_manual_reply_arriving_during_backfill(self) -> None:
        exact_head = "1700000000.999999"
        self.opener.open.side_effect = [
            response({"ok": True, "messages": [{"ts": "1700000000.999998", "text": "older"},
                                                {"ts": exact_head, "text": "1362", "user": "UBOT"}],
                      "has_more": True, "response_metadata": {"next_cursor": "backfill"}}),
            response({"ok": True, "messages": [{"ts": "1699999999.999999", "text": "old"}],
                      "has_more": False}),
            response({"ok": True, "messages": [
                {"ts": "1700000001.000001", "text": "1362", "user": "UMYSELF"},
                {"ts": exact_head, "text": "1362", "user": "UBOT", "updated": True},
            ], "has_more": False}),
        ]
        messages = self.client.history("D123", oldest=1699990000)
        self.assertEqual(len(messages), 4)
        self.assertIn({"ts": "1700000001.000001", "text": "1362", "user": "UMYSELF"}, messages)
        self.assertTrue(next(message for message in messages if message["ts"] == exact_head)["updated"])
        refresh = form_parameters(self.opener.open.call_args_list[-1].args[0])
        self.assertEqual(refresh["oldest"], exact_head)
        self.assertEqual(refresh["inclusive"], "true")
        self.assertNotIn("cursor", refresh)

    def test_busy_head_refresh_aborts_with_transient_error(self) -> None:
        for overflow in ({"has_more": True}, {"has_more": False,
                                               "response_metadata": {"next_cursor": "more-new"}}):
            with self.subTest(overflow=overflow):
                self.opener.open.side_effect = [
                    response({"ok": True, "messages": [{"ts": "3.001"}],
                              "response_metadata": {"next_cursor": "older"}}),
                    response({"ok": True, "messages": [{"ts": "1.001"}], "has_more": False}),
                    response({"ok": True, "messages": [{"ts": "4.001"}], **overflow}),
                ]
                with self.assertRaises(SlackError) as caught:
                    self.client.history("D123", oldest=0)
                self.assertEqual(caught.exception.code, "busy_conversation")
                self.assertFalse(caught.exception.permanent)

    def test_single_page_history_does_not_refresh(self) -> None:
        self.opener.open.return_value = response({"ok": True, "messages": [{"ts": "1.001"}]})
        self.assertEqual(self.client.history("D123", oldest=0), [{"ts": "1.001"}])
        self.opener.open.assert_called_once()

    def test_head_refresh_uses_same_sixty_second_history_gate(self) -> None:
        stop = Mock(spec=threading.Event)
        stop.wait.return_value = False
        client = SlackClient("xoxp-test", stop=stop)
        client._opener = self.opener
        self.opener.open.side_effect = [
            response({"ok": True, "messages": [{"ts": "3.001"}],
                      "response_metadata": {"next_cursor": "older"}}),
            response({"ok": True, "messages": [{"ts": "1.001"}]}),
            response({"ok": True, "messages": [{"ts": "3.001"}]}),
        ]
        with patch("attendance.slack.time.monotonic", side_effect=[100, 100, 100, 160, 160, 220]):
            client.history("D123", oldest=0)
        self.assertEqual(stop.wait.call_args_list, [call(0.0), call(60.0), call(60.0)])
        self.assertEqual(client._history_next, 280)

    def test_history_retry_after_extends_the_gate_for_next_attempt(self) -> None:
        stop = Mock(spec=threading.Event)
        stop.wait.return_value = False
        client = SlackClient("xoxp-test", stop=stop)
        client._opener = self.opener
        headers = Message()
        headers["Retry-After"] = "90"
        self.opener.open.side_effect = [
            urllib.error.HTTPError("https://slack.com/api/conversations.history", 429,
                                   "limited", headers, io.BytesIO()),
            response({"ok": True, "messages": []}),
        ]
        with patch("attendance.slack.time.monotonic", side_effect=[100, 100, 100, 110, 190]):
            with self.assertRaises(SlackError):
                client.history("D123", oldest=0)
            client.history("D123", oldest=0)
        self.assertEqual(stop.wait.call_args_list, [call(0.0), call(80.0)])
        self.assertEqual(client._history_next, 250)

    def test_incomplete_history_never_returns_partial_messages(self) -> None:
        self.opener.open.return_value = response(
            {"ok": True, "messages": [{"ts": "1.001"}], "has_more": True,
             "response_metadata": {"next_cursor": ""}}
        )
        with self.assertRaises(SlackError) as caught:
            self.client.history("D123", oldest=0)
        self.assertEqual(caught.exception.code, "incomplete_history")
        self.assertTrue(caught.exception.permanent)

    def test_repeated_cursor_never_returns_partial_messages(self) -> None:
        self.opener.open.return_value = response(
            {"ok": True, "messages": [{"ts": "1.001"}], "has_more": True,
             "response_metadata": {"next_cursor": "repeat"}}
        )
        with self.assertRaises(SlackError) as caught:
            self.client.history("D123", oldest=0)
        self.assertEqual(caught.exception.code, "invalid_cursor")
        self.assertEqual(self.opener.open.call_count, 2)

    def test_invalid_history_messages_are_rejected(self) -> None:
        for messages in (None, {}, [None], [{"text": "missing timestamp"}], [{"ts": 1.001}]):
            with self.subTest(messages=messages):
                self.opener.open.return_value = response(
                    {"ok": True, "messages": messages, "has_more": False}
                )
                with self.assertRaises(SlackError) as caught:
                    self.client.history("D123", oldest=0)
                self.assertEqual(caught.exception.code, "invalid_response")

    def test_invalid_history_metadata_is_rejected(self) -> None:
        for metadata in (None, [], "cursor"):
            with self.subTest(metadata=metadata):
                self.opener.open.return_value = response(
                    {"ok": True, "messages": [], "has_more": False,
                     "response_metadata": metadata}
                )
                with self.assertRaises(SlackError):
                    self.client.history("D123", oldest=0)

    def test_stopped_history_performs_no_request(self) -> None:
        self.client.stop.set()
        with self.assertRaises(InterruptedError):
            self.client.history("D123", oldest=0)
        self.opener.open.assert_not_called()

    def test_retry_after_values_are_bounded_and_finite(self) -> None:
        for value, expected in (
            (None, 60), ("", 60), ("invalid", 60), ("NaN", 60),
            ("inf", 60), ("-10", 1), ("0", 1), ("1.5", 1.5), ("180", 180)
        ):
            with self.subTest(value=value):
                self.assertEqual(retry_seconds(value), expected)


if __name__ == "__main__":
    unittest.main()
