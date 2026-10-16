import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from scripts import proxy_handler as proxy


NODE_ID = "11111111-1111-4111-8111-111111111111"


def b64(value):
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def fixture_urls():
    return [
        f"vless://{NODE_ID}@node.example.com:443?security=tls&type=ws&host=cdn.example.com&path=%2Fws%3Fed%3D2048",
        f"vless://{NODE_ID}@node.example.com:443?security=reality&pbk={b64('a' * 32)}&sid=abcd&flow=xtls-rprx-vision",
        "trojan://fake%3Apassword@node.example.com:443?type=grpc&serviceName=checkin&sni=cdn.example.com",
        "vmess://" + b64(json.dumps({"add": "node.example.com", "port": "443", "id": NODE_ID, "tls": "tls", "net": "ws", "path": "/ws", "host": "cdn.example.com"})),
        "ss://" + b64("aes-128-gcm:fake-password") + "@node.example.com:8388#example",
        "ss://" + b64("chacha20-ietf-poly1305:fake-password@[2001:db8::1]:8388"),
        f"vless://{NODE_ID}@node.example.com:443?security=tls&type=h2&host=cdn.example.com&path=%2Fhttp",
    ]


class NodeParsingTests(unittest.TestCase):
    def test_vless_websocket_tls_and_early_data(self):
        outbound = proxy.build_outbound(fixture_urls()[0])
        self.assertEqual(outbound["type"], "vless")
        self.assertEqual(outbound["uuid"], NODE_ID)
        self.assertEqual(outbound["tls"]["server_name"], "cdn.example.com")
        self.assertEqual(outbound["transport"], {
            "type": "ws", "path": "/ws", "headers": {"Host": "cdn.example.com"},
            "max_early_data": 2048, "early_data_header_name": "Sec-WebSocket-Protocol",
        })

    def test_reality_and_trojan_grpc_credentials(self):
        reality = proxy.build_outbound(fixture_urls()[1])
        self.assertEqual(reality["flow"], "xtls-rprx-vision")
        self.assertEqual(reality["tls"]["reality"]["short_id"], "abcd")
        trojan = proxy.build_outbound(fixture_urls()[2])
        self.assertEqual(trojan["password"], "fake:password")
        self.assertEqual(trojan["transport"], {"type": "grpc", "service_name": "checkin"})

    def test_websocket_uses_sni_as_host_when_host_is_not_supplied(self):
        outbound = proxy.build_outbound(
            f"vless://{NODE_ID}@node.example.com:443?security=tls&type=ws&sni=cdn.example.com"
        )
        self.assertEqual(outbound["transport"]["headers"], {"Host": "cdn.example.com"})

    def test_vmess_and_both_shadowsocks_link_formats(self):
        vmess, sip002, legacy = [proxy.build_outbound(url) for url in fixture_urls()[3:6]]
        self.assertEqual(vmess["uuid"], NODE_ID)
        self.assertEqual(vmess["transport"]["path"], "/ws")
        self.assertEqual(sip002["password"], "fake-password")
        self.assertEqual(sip002["server_port"], 8388)
        self.assertEqual(legacy["server"], "2001:db8::1")

    def test_configuration_binds_only_loopback_and_has_no_direct_outbound(self):
        config = proxy.build_config(fixture_urls()[0])
        self.assertEqual(config["inbounds"][0]["listen"], "127.0.0.1")
        self.assertEqual(config["inbounds"][0]["listen_port"], 8118)
        self.assertEqual(config["route"], {"final": "proxy"})
        self.assertEqual(len(config["outbounds"]), 1)

    def test_bad_nodes_fail_without_echoing_credentials(self):
        urls = [
            "", "https://private-secret@node.example.com", "vmess://private-secret",
            "ss://" + b64("aes-128-gcm:private-secret") + "@node.example.com:0",
            f"vless://{NODE_ID}@node.example.com:0",
            f"vless://{NODE_ID}@node.example.com:443?type=unsupported&secret=private-secret",
            f"vless://{NODE_ID}@node.example.com:443?security=reality&secret=private-secret",
        ]
        for index, url in enumerate(urls):
            with self.subTest(index=index), self.assertRaises(proxy.ProxyError) as caught:
                proxy.build_config(url)
            self.assertNotIn("private-secret", str(caught.exception))
            self.assertNotIn("node.example.com", str(caught.exception))


class ProxyLifecycleTests(unittest.TestCase):
    def test_no_node_does_not_download_or_change_environment(self):
        with patch.object(proxy, "install_sing_box") as install, patch.object(proxy, "_listener_open") as listener:
            with contextlib.redirect_stdout(io.StringIO()):
                proxy.start_proxy({})
        install.assert_not_called()
        listener.assert_not_called()

    def test_ready_proxy_publishes_environment_and_is_stopped_and_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "github-env"
            env = {"PROXY_URL": fixture_urls()[0], "RUNNER_TEMP": directory, "GITHUB_ENV": str(env_file)}
            process = Mock(pid=123456, poll=Mock(return_value=None))
            with patch.object(proxy, "install_sing_box", return_value=Path(directory) / "sing-box"):
                with patch.object(proxy.subprocess, "run", return_value=Mock(returncode=0)):
                    with patch.object(proxy.subprocess, "Popen", return_value=process):
                        with patch.object(proxy, "_listener_open", side_effect=[False, True]):
                            with contextlib.redirect_stdout(io.StringIO()):
                                proxy.start_proxy(env)
            self.assertEqual(env_file.read_text(), f"PROXY_SERVER={proxy.PROXY_SERVER}\n")
            runtime = proxy._runtime_directory(env)
            self.assertEqual(json.loads((runtime / "config.json").read_text())["route"]["final"], "proxy")
            with patch.object(proxy.os, "kill") as kill, contextlib.redirect_stdout(io.StringIO()):
                proxy.stop_proxy(env)
            kill.assert_called_once_with(process.pid, proxy.signal.SIGTERM)
            self.assertFalse((runtime / "config.json").exists())
            self.assertFalse((runtime / "proxy.pid").exists())
            self.assertFalse((runtime / "sing-box.log").exists())

    def test_dead_proxy_never_publishes_a_direct_or_proxy_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "github-env"
            env = {"PROXY_URL": fixture_urls()[0], "RUNNER_TEMP": directory, "GITHUB_ENV": str(env_file)}
            process = Mock(pid=123456, poll=Mock(return_value=1))
            with patch.object(proxy, "install_sing_box", return_value=Path(directory) / "sing-box"):
                with patch.object(proxy.subprocess, "run", return_value=Mock(returncode=0)):
                    with patch.object(proxy.subprocess, "Popen", return_value=process):
                        with patch.object(proxy, "_listener_open", return_value=False):
                            with self.assertRaises(proxy.ProxyError):
                                proxy.start_proxy(env)
            self.assertFalse(env_file.exists())
            self.assertFalse((proxy._runtime_directory(env) / "proxy.pid").exists())

    def test_download_integrity_failure_rejects_binary(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"tampered-archive"
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(proxy.platform, "system", return_value="Linux"), patch.object(proxy.platform, "machine", return_value="x86_64"):
                with patch.object(proxy, "urlopen", return_value=response):
                    with self.assertRaisesRegex(proxy.ProxyError, "checksum"):
                        proxy.install_sing_box(Path(directory))
            self.assertFalse((Path(directory) / "sing-box").exists())

    def test_invalid_sing_box_configuration_never_starts_or_publishes_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "github-env"
            env = {"PROXY_URL": fixture_urls()[0], "RUNNER_TEMP": directory, "GITHUB_ENV": str(env_file)}
            with patch.object(proxy, "install_sing_box", return_value=Path(directory) / "sing-box"):
                with patch.object(proxy.subprocess, "run", return_value=Mock(returncode=1)):
                    with patch.object(proxy.subprocess, "Popen") as launch:
                        with patch.object(proxy, "_listener_open", return_value=False):
                            with self.assertRaisesRegex(proxy.ProxyError, "rejected"):
                                proxy.start_proxy(env)
            launch.assert_not_called()
            self.assertFalse(env_file.exists())
            with contextlib.redirect_stdout(io.StringIO()):
                proxy.stop_proxy(env)
            self.assertFalse((proxy._runtime_directory(env) / "config.json").exists())

    def test_verified_download_extracts_only_the_expected_executable(self):
        content = b"fake executable"
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as package:
            member = tarfile.TarInfo(f"sing-box-{proxy.SING_BOX_VERSION}-linux-amd64/sing-box")
            member.size = len(content)
            package.addfile(member, io.BytesIO(content))
        data = archive.getvalue()
        response = MagicMock()
        response.__enter__.return_value.read.return_value = data
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(proxy.platform, "system", return_value="Linux"), patch.object(proxy.platform, "machine", return_value="x86_64"):
                with patch.object(proxy, "urlopen", return_value=response), patch.object(proxy, "SING_BOX_SHA256", hashlib.sha256(data).hexdigest()):
                    binary = proxy.install_sing_box(Path(directory))
            self.assertEqual(binary.read_bytes(), content)

    def test_cli_redacts_even_unexpected_errors(self):
        output = io.StringIO()
        with patch.object(proxy, "start_proxy", side_effect=RuntimeError("private-secret node.example.com")):
            with contextlib.redirect_stderr(output):
                self.assertEqual(proxy.main(["start"]), 1)
        self.assertNotIn("private-secret", output.getvalue())
        self.assertNotIn("node.example.com", output.getvalue())


@unittest.skipUnless(os.environ.get("SING_BOX_BINARY"), "Optional actual sing-box config validation")
class SingBoxCompatibilityTests(unittest.TestCase):
    def test_all_supported_protocols_and_transports_validate(self):
        for index, url in enumerate(fixture_urls()):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                path.write_text(json.dumps(proxy.build_config(url)), encoding="utf-8")
                result = subprocess.run(
                    [os.environ["SING_BOX_BINARY"], "check", "-c", str(path)],
                    capture_output=True, timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))


if __name__ == "__main__":
    unittest.main()
