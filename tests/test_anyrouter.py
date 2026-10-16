import importlib.util
import sys
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch


def load_app():
    requests = types.ModuleType("requests")
    requests.Session = Mock()
    playwright = types.ModuleType("playwright")
    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = Mock()
    path = Path(__file__).resolve().parents[1] / "AnyRouter" / "app.py"
    spec = importlib.util.spec_from_file_location("anyrouter_test_app", path)
    app = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        "requests": requests,
        "playwright": playwright,
        "playwright.sync_api": sync_api,
    }):
        spec.loader.exec_module(app)
    return app


class CheckinExitStatusTests(unittest.TestCase):
    def setUp(self):
        self.app = load_app()
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.app.SESSION = "example-session"
        self.app.USER_ID = "123456"
        for name, value in {
            "log": Mock(),
            "get_waf_cookies": Mock(return_value={}),
            "get_user_info": Mock(return_value={"quota": 500000, "username": "example"}),
            "check_session_expiry": Mock(return_value=(20, False)),
            "send_telegram": Mock(return_value=False),
            "do_check_in": Mock(return_value=True),
        }.items():
            self.patches.enter_context(patch.object(self.app, name, value))
        self.patches.enter_context(patch.object(self.app.time, "sleep"))

    def test_failed_checkin_exits_nonzero_after_reporting(self):
        self.app.do_check_in.return_value = False
        with self.assertRaises(SystemExit) as result:
            self.app.main()
        self.assertEqual(result.exception.code, 1)
        self.app.send_telegram.assert_called_once()
        self.assertIn("签到结果: 失败", self.app.send_telegram.call_args.args[0])
        self.assertEqual(self.app.get_user_info.call_count, 2)

    def test_success_does_not_fail_when_standalone_notification_is_disabled(self):
        self.assertIsNone(self.app.main())
        self.app.do_check_in.assert_called_once()
        self.app.send_telegram.assert_called_once()
        self.assertIn("签到结果: 成功或今日已签到", self.app.send_telegram.call_args.args[0])

    def test_invalid_session_fails_without_attempting_checkin(self):
        self.app.get_user_info.return_value = None
        with self.assertRaises(SystemExit) as result:
            self.app.main()
        self.assertEqual(result.exception.code, 1)
        self.app.do_check_in.assert_not_called()


if __name__ == "__main__":
    unittest.main()
