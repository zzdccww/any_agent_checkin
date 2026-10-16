import importlib.util
import sys
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch


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
        self.app.PROXY_SERVER = ""
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

    def test_proxy_routes_all_api_requests_without_environment_bypass(self):
        self.app.PROXY_SERVER = "http://127.0.0.1:8118"
        self.app.main()
        session = self.app.requests.Session.return_value
        self.assertIs(session.trust_env, False)
        session.proxies.update.assert_called_once_with({
            "http": self.app.PROXY_SERVER, "https": self.app.PROXY_SERVER,
        })

    def test_direct_mode_does_not_override_session_proxy_settings(self):
        self.app.main()
        self.app.requests.Session.return_value.proxies.update.assert_not_called()


class BrowserProxyTests(unittest.TestCase):
    def test_browser_and_api_use_the_same_proxy_endpoint(self):
        for proxy_server in ("", "http://127.0.0.1:8118"):
            with self.subTest(proxy_configured=bool(proxy_server)):
                app = load_app()
                app.PROXY_SERVER = proxy_server
                app.log = Mock()
                manager = MagicMock()
                app.sync_playwright = Mock(return_value=manager)
                browser = manager.__enter__.return_value.chromium.launch.return_value
                browser.new_context.return_value.cookies.return_value = []
                app.get_waf_cookies()
                options = manager.__enter__.return_value.chromium.launch.call_args.kwargs
                if proxy_server:
                    self.assertEqual(options["proxy"], {"server": proxy_server})
                else:
                    self.assertNotIn("proxy", options)
                browser.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
