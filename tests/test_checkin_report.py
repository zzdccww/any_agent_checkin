import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from scripts import checkin_report as reporting


ENV = {
    "AGENT_ROUTER_RESULT": "success",
    "ANYROUTER_RESULT": "failure",
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_REPOSITORY": "example/checkin",
    "GITHUB_RUN_ID": "12345",
    "REPORT_TIME": "2026-10-15T23:07:00Z",
}


class ReportFormattingTests(unittest.TestCase):
    def test_mixed_results_share_beijing_time_and_run_link(self):
        report = reporting.load_report(ENV)
        self.assertEqual(report.beijing_time, "2026-10-16 07:07:00 (UTC+8)")
        for output in (report.telegram_text(), report.markdown()):
            self.assertIn("Agent Router", output)
            self.assertIn("AnyRouter", output)
            self.assertIn("success", output)
            self.assertIn("failure", output)
            self.assertIn("https://github.com/example/checkin/actions/runs/12345", output)

    def test_cancelled_skipped_and_unknown_statuses(self):
        for result in ("cancelled", "skipped", "success", "failure"):
            with self.subTest(result=result):
                report = reporting.load_report({**ENV, "AGENT_ROUTER_RESULT": result})
                self.assertIn(f"({result})", report.telegram_text())
        report = reporting.load_report({**ENV, "ANYROUTER_RESULT": "private-account@example.com"})
        self.assertIn("unknown", report.telegram_text())
        self.assertNotIn("private-account", report.telegram_text())

    def test_run_metadata_rejects_credentials_and_markdown_injection(self):
        for change in (
            {"GITHUB_SERVER_URL": "https://secret@github.com"},
            {"GITHUB_REPOSITORY": "example/checkin)\nprivate"},
            {"GITHUB_RUN_ID": "123?token=private"},
        ):
            with self.subTest(change=change), self.assertRaises(reporting.ReportError):
                reporting.load_report({**ENV, **change})

    def test_timestamp_requires_timezone(self):
        with self.assertRaises(reporting.ReportError):
            reporting.load_report({**ENV, "REPORT_TIME": "2026-10-16T08:07:00"})


class ReadmeUpdateTests(unittest.TestCase):
    def test_preserves_all_bytes_outside_results_block(self):
        prefix = "# 文档\r\n部署说明。\r\n".encode("utf-8") + reporting.START_MARKER
        suffix = reporting.END_MARKER + "\r\n## 配置\r\n配置说明。\r\n".encode("utf-8")
        content = prefix + b"\r\nold result\r\n" + suffix
        updated = reporting.replace_results(content, reporting.load_report(ENV))
        self.assertTrue(updated.startswith(prefix))
        self.assertTrue(updated.endswith(suffix))
        self.assertNotIn(b"old result", updated)
        self.assertNotIn(b"\n", updated.replace(b"\r\n", b""))
        self.assertEqual(reporting.replace_results(updated, reporting.load_report(ENV)), updated)

    def test_invalid_markers_fail_without_overwriting_documentation(self):
        for content in (
            b"# Guide with no results markers",
            reporting.START_MARKER + reporting.START_MARKER + reporting.END_MARKER,
            reporting.END_MARKER + reporting.START_MARKER,
        ):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "README.md"
                path.write_bytes(content)
                with self.assertRaises(reporting.ReportError):
                    reporting.update_readme(path, reporting.load_report(ENV))
                self.assertEqual(path.read_bytes(), content)

    def test_cli_update_readme_persists_marked_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "README.md"
            path.write_bytes(b"# Guide\n" + reporting.START_MARKER + b"\nold\n" + reporting.END_MARKER)
            with patch.dict("os.environ", ENV, clear=True), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(reporting.main(["update-readme", "--readme", str(path)]), 0)
            self.assertIn(b"12345", path.read_bytes())
            self.assertTrue(path.read_bytes().startswith(b"# Guide\n"))


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.report = reporting.load_report(ENV)
        self.token = "123456:fake-bot-secret"
        self.chat_id = "987654"

    def test_missing_either_setting_skips_without_network_request(self):
        for token, chat_id in (("", self.chat_id), (self.token, ""), (" ", " ")):
            with self.subTest(token_present=bool(token.strip()), chat_present=bool(chat_id.strip())):
                output = io.StringIO()
                with patch.object(reporting, "urlopen") as send, contextlib.redirect_stdout(output):
                    self.assertFalse(reporting.send_telegram(self.report, token, chat_id))
                send.assert_not_called()
                self.assertIn("skipped", output.getvalue())
                self.assertNotIn(self.token, output.getvalue())

    def test_sends_exactly_one_plain_text_post_with_both_service_results(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok": true}'
        with patch.object(reporting, "urlopen", return_value=response) as send:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(reporting.send_telegram(self.report, self.token, self.chat_id))
        send.assert_called_once()
        request = send.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.method, "POST")
        self.assertEqual(payload["chat_id"], self.chat_id)
        self.assertEqual(payload["text"], self.report.telegram_text())
        self.assertNotIn("parse_mode", payload)
        self.assertEqual(send.call_args.kwargs["timeout"], reporting.HTTP_TIMEOUT)

    def test_cli_error_diagnostics_never_expose_credentials_or_bot_url(self):
        bot_url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        errors = (
            HTTPError(bot_url, 401, self.token, None, None),
            URLError(f"Unable to reach {bot_url}; chat {self.chat_id}"),
            TimeoutError(bot_url),
        )
        for error in errors:
            output = io.StringIO()
            with self.subTest(error=type(error).__name__), patch.dict(
                "os.environ", {**ENV, "TG_BOT_TOKEN": self.token, "TG_CHAT_ID": self.chat_id}, clear=True
            ):
                with patch.object(reporting, "urlopen", side_effect=error):
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                        self.assertEqual(reporting.main(["notify"]), 1)
            if isinstance(error, HTTPError):
                self.assertTrue(error.closed)
            self.assertIn("Telegram notification failed", output.getvalue())
            for sensitive in (self.token, self.chat_id, "api.telegram.org"):
                self.assertNotIn(sensitive, output.getvalue())

    def test_api_rejection_and_invalid_json_are_failures_without_response_details(self):
        for body in (
            json.dumps({"ok": False, "description": self.token}).encode(),
            self.token.encode(),
            b"[]",
            b"x" * (reporting.MAX_RESPONSE_BYTES + 1),
        ):
            response = MagicMock()
            response.__enter__.return_value.read.return_value = body
            with self.subTest(body_size=len(body)), patch.object(reporting, "urlopen", return_value=response):
                with self.assertRaises(reporting.ReportError) as caught:
                    reporting.send_telegram(self.report, self.token, self.chat_id)
                self.assertNotIn(self.token, str(caught.exception))

    def test_notify_mode_does_not_depend_on_readme_access(self):
        with patch.dict("os.environ", ENV, clear=True), patch.object(Path, "read_bytes") as read:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(reporting.main(["notify"]), 0)
        read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
