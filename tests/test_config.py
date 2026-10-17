import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from attendance.config import Config, ConfigurationError, load_env


BASE_ENV = {
    "SLACK_USER_TOKEN": "xoxp-test-user-token",
    "SLACK_ATTENDANCE_CHANNEL_ID": "D123ABC",
    "SLACK_ATTENDANCE_SENDER_ID": "B123ABC",
    "SLACK_EXPECTED_USER_ID": "U456DEF",
}


class ConfigTests(unittest.TestCase):
    def test_valid_configuration_defaults_and_private_dm_target(self) -> None:
        with patch.dict(os.environ, BASE_ENV, clear=True):
            config = Config.from_env()
        self.assertEqual(config.channel, "D123ABC")
        self.assertEqual(config.sender, "B123ABC")
        self.assertEqual(config.expected_user, "U456DEF")
        self.assertEqual(str(config.timezone), "Asia/Bangkok")
        self.assertEqual(config.state_path, Path("data/state.sqlite3"))
        self.assertEqual(config.poll_seconds, 60)
        self.assertEqual(config.timeout_seconds, 15)
        config.require_target(live=True)

    def test_custom_configuration_and_interval_boundaries(self) -> None:
        for poll, timeout in ((60, 5), (600, 60)):
            with self.subTest(poll=poll, timeout=timeout):
                environment = dict(
                    BASE_ENV, POLL_INTERVAL_SECONDS=str(poll), HTTP_TIMEOUT_SECONDS=str(timeout),
                    SLACK_TIMEZONE="Asia/Shanghai", ATTENDANCE_STATE_PATH="/tmp/attendance.sqlite3",
                    SLACK_ATTENDANCE_SENDER_ID="U123ABC", SLACK_EXPECTED_USER_ID="W456DEF",
                )
                with patch.dict(os.environ, environment, clear=True):
                    config = Config.from_env()
                self.assertEqual(config.poll_seconds, poll)
                self.assertEqual(config.timeout_seconds, timeout)
                self.assertEqual(str(config.timezone), "Asia/Shanghai")
                self.assertEqual(config.state_path, Path("/tmp/attendance.sqlite3"))

    def test_bot_token_missing_token_and_whitespace_are_rejected(self) -> None:
        for token in ("", "xoxb-bot-token", "xapp-app-token", "xoxp-invalid token", "xoxp-invalid\ntoken"):
            with self.subTest(token=token):
                with patch.dict(os.environ, dict(BASE_ENV, SLACK_USER_TOKEN=token), clear=True):
                    with self.assertRaises(ConfigurationError):
                        Config.from_env()

    def test_invalid_intervals_and_timeouts_are_rejected(self) -> None:
        for name, values in (
            ("POLL_INTERVAL_SECONDS", ("0", "59", "601", "nan", "60.5", "")),
            ("HTTP_TIMEOUT_SECONDS", ("0", "4", "61", "NaN", "5.5", "")),
        ):
            for value in values:
                with self.subTest(name=name, value=value):
                    with patch.dict(os.environ, dict(BASE_ENV, **{name: value}), clear=True):
                        with self.assertRaises(ConfigurationError):
                            Config.from_env()

    def test_invalid_target_identifiers_are_rejected(self) -> None:
        for name, values in (
            ("SLACK_ATTENDANCE_CHANNEL_ID", ("C123ABC", "D", "D123/name", "d123")),
            ("SLACK_ATTENDANCE_SENDER_ID", ("C123ABC", "B", "U123 name", "bot")),
            ("SLACK_EXPECTED_USER_ID", ("B123ABC", "U", "W123/name", "user")),
        ):
            for value in values:
                with self.subTest(name=name, value=value):
                    with patch.dict(os.environ, dict(BASE_ENV, **{name: value}), clear=True):
                        with self.assertRaises(ConfigurationError):
                            Config.from_env()

    def test_inspection_allows_unknown_sender_but_live_requires_verified_target(self) -> None:
        for missing in ("SLACK_ATTENDANCE_SENDER_ID", "SLACK_EXPECTED_USER_ID"):
            environment = BASE_ENV.copy()
            environment.pop(missing)
            with self.subTest(missing=missing):
                with patch.dict(os.environ, environment, clear=True):
                    config = Config.from_env()
                config.require_target(live=False)
                with self.assertRaises(ConfigurationError):
                    config.require_target(live=True)

    def test_missing_channel_is_rejected_for_inspection_and_live(self) -> None:
        environment = BASE_ENV.copy()
        environment.pop("SLACK_ATTENDANCE_CHANNEL_ID")
        with patch.dict(os.environ, environment, clear=True):
            config = Config.from_env()
        for live in (False, True):
            with self.subTest(live=live):
                with self.assertRaises(ConfigurationError):
                    config.require_target(live=live)

    def test_invalid_timezone_is_rejected(self) -> None:
        for timezone in ("Not/A_Zone", "../Asia/Bangkok", ""):
            with self.subTest(timezone=timezone):
                with patch.dict(os.environ, dict(BASE_ENV, SLACK_TIMEZONE=timezone), clear=True):
                    with self.assertRaises(ConfigurationError):
                        Config.from_env()


class DotEnvTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / ".env"

    def test_values_are_literal_and_never_execute_shell_commands(self) -> None:
        marker = self.directory / "executed"
        literal = f"$(touch {marker}) ${{HOME}} `touch {marker}`"
        self.path.write_text(
            f"# comment\n\nLITERAL='{literal}'\nDOUBLE=\"a b\"\n"
            "EQUALS=a=b=c\nBACKSLASH=a\\nb\nEMPTY=\n", encoding="utf-8"
        )
        with patch.dict(os.environ, {}, clear=True):
            load_env(self.path)
            self.assertEqual(os.environ["LITERAL"], literal)
            self.assertEqual(os.environ["DOUBLE"], "a b")
            self.assertEqual(os.environ["EQUALS"], "a=b=c")
            self.assertEqual(os.environ["BACKSLASH"], "a\\nb")
            self.assertEqual(os.environ["EMPTY"], "")
        self.assertFalse(marker.exists())

    def test_existing_environment_wins_over_file(self) -> None:
        self.path.write_text("SLACK_USER_TOKEN=xoxp-file\nNEW_VALUE=added\n", encoding="utf-8")
        with patch.dict(os.environ, {"SLACK_USER_TOKEN": "xoxp-environment"}, clear=True):
            load_env(self.path)
            self.assertEqual(os.environ["SLACK_USER_TOKEN"], "xoxp-environment")
            self.assertEqual(os.environ["NEW_VALUE"], "added")

    def test_missing_env_file_is_allowed(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            load_env(self.path)
            self.assertEqual(dict(os.environ), {})

    def test_malformed_pairs_and_unbalanced_quotes_are_rejected(self) -> None:
        for line in ("MISSING_EQUALS", "export TOKEN=value", "lowercase=value", "A='broken", 'A="broken'):
            with self.subTest(line=line):
                self.path.write_text(line, encoding="utf-8")
                with patch.dict(os.environ, {}, clear=True):
                    with self.assertRaises(ConfigurationError):
                        load_env(self.path)


if __name__ == "__main__":
    unittest.main()
