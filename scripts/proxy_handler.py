"""Convert node sharing URLs to a local sing-box proxy for check-in jobs."""

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import parse_qs, unquote, urlsplit
from urllib.request import urlopen
from uuid import UUID


LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8118
PROXY_SERVER = f"http://{LISTEN_HOST}:{LISTEN_PORT}"
SING_BOX_VERSION = "1.14.3"
SING_BOX_SHA256 = "e2bdf179c15a3652955dc44867e33ca0965c9c9b91b34267dcc5a5d639a5feee"


class ProxyError(Exception):
    """Only credential-free messages may be exposed by this exception."""


def _b64decode(value):
    value = unquote(value).strip()
    value += "=" * (-len(value) % 4)
    return base64.b64decode(value, altchars=b"-_", validate=True).decode("utf-8")


def _port(value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise ValueError("Invalid port")
    return port


def _query(parsed):
    return {key: values[0] for key, values in parse_qs(parsed.query).items()}


def _tls_block(security, query, server):
    if security in ("", "none"):
        return None
    if security not in ("tls", "reality", "xtls"):
        raise ValueError("Unsupported TLS mode")
    tls = {
        "enabled": True,
        "server_name": query.get("sni") or query.get("host") or server,
        "utls": {"enabled": True, "fingerprint": query.get("fp") or "chrome"},
    }
    if query.get("allowInsecure", "0").lower() in ("1", "true"):
        tls["insecure"] = True
    if query.get("alpn"):
        tls["alpn"] = [part.strip() for part in query["alpn"].split(",") if part.strip()]
    if security == "reality":
        if not query.get("pbk"):
            raise ValueError("Missing Reality public key")
        tls["reality"] = {"enabled": True, "public_key": query["pbk"], "short_id": query.get("sid", "")}
    return tls


def _transport_block(network, query):
    if network in ("", "tcp", "raw"):
        return None
    if network == "ws":
        path, _, extra_query = query.get("path", "/").partition("?")
        transport = {"type": "ws", "path": path or "/"}
        host = query.get("host") or query.get("sni")
        if host:
            transport["headers"] = {"Host": host}
        early_data = parse_qs(extra_query).get("ed", [None])[0]
        if early_data is not None:
            size = int(early_data)
            if size < 0:
                raise ValueError("Invalid early data size")
            transport.update(max_early_data=size, early_data_header_name="Sec-WebSocket-Protocol")
        return transport
    if network == "grpc":
        return {"type": "grpc", "service_name": query.get("serviceName") or query.get("path", "")}
    if network in ("http", "h2"):
        transport = {"type": "http", "path": query.get("path", "/")}
        if query.get("host"):
            transport["host"] = query["host"].split(",")
        return transport
    raise ValueError("Unsupported transport")


def _url_outbound(url, protocol):
    parsed = urlsplit(url)
    if not parsed.hostname or not parsed.username:
        raise ValueError("Missing node address or credentials")
    outbound = {
        "type": protocol, "tag": "proxy", "server": parsed.hostname,
        "server_port": _port(443 if parsed.port is None else parsed.port),
    }
    credential = unquote(parsed.netloc.rsplit("@", 1)[0])
    outbound["uuid" if protocol == "vless" else "password"] = (
        str(UUID(credential)) if protocol == "vless" else credential
    )
    query = _query(parsed)
    if protocol == "vless" and query.get("flow"):
        outbound["flow"] = query["flow"]
    security = query.get("security", "tls" if protocol == "trojan" else "none")
    tls = _tls_block("tls" if protocol == "trojan" and security == "none" else security, query, parsed.hostname)
    transport = _transport_block(query.get("type", "tcp"), query)
    if tls:
        outbound["tls"] = tls
    if transport:
        outbound["transport"] = transport
    return outbound


def parse_vmess(url):
    node = json.loads(_b64decode(url[len("vmess://"):].split("#", 1)[0]))
    if not isinstance(node, dict) or not node.get("add") or not node.get("id"):
        raise ValueError("Missing VMess fields")
    outbound = {
        "type": "vmess", "tag": "proxy", "server": node["add"],
        "server_port": _port(node.get("port", 443)), "uuid": str(UUID(node["id"])),
        "alter_id": int(node.get("aid") or 0), "security": node.get("scy") or "auto",
    }
    query = {key: str(node.get(key) or "") for key in ("sni", "host", "alpn", "fp", "pbk", "sid")}
    query["path"] = node.get("path") or "/"
    query["serviceName"] = node.get("path") or ""
    query["allowInsecure"] = str(node.get("allowInsecure", "0"))
    tls = _tls_block(node.get("tls") or "none", query, node["add"])
    transport = _transport_block(node.get("net") or "tcp", query)
    if tls:
        outbound["tls"] = tls
    if transport:
        outbound["transport"] = transport
    return outbound


def parse_ss(url):
    parsed = urlsplit(url)
    if "plugin" in _query(parsed):
        raise ValueError("Shadowsocks plugins are unsupported")
    body = url[len("ss://"):].split("#", 1)[0].split("?", 1)[0].rstrip("/")
    if "@" in body:
        credential, address = body.rsplit("@", 1)
        credential = unquote(credential)
        if ":" not in credential:
            credential = _b64decode(credential)
    else:
        credential, address = _b64decode(body).rsplit("@", 1)
    method, password = credential.split(":", 1)
    server = urlsplit("//" + address)
    if not method or not password or not server.hostname or server.port is None:
        raise ValueError("Missing Shadowsocks fields")
    return {
        "type": "shadowsocks", "tag": "proxy", "server": server.hostname,
        "server_port": _port(server.port), "method": method, "password": password,
    }


def build_outbound(url):
    try:
        if url.startswith("vless://"):
            return _url_outbound(url, "vless")
        if url.startswith("trojan://"):
            return _url_outbound(url, "trojan")
        if url.startswith("vmess://"):
            return parse_vmess(url)
        if url.startswith("ss://"):
            return parse_ss(url)
    except Exception:
        raise ProxyError("Invalid PROXY_URL node or unsupported transport/TLS settings.") from None
    raise ProxyError("PROXY_URL supports only vless://, vmess://, trojan:// and ss:// nodes.")


def build_config(url):
    return {
        "log": {"level": "error"},
        "inbounds": [{"type": "mixed", "tag": "mixed-in", "listen": LISTEN_HOST, "listen_port": LISTEN_PORT}],
        "outbounds": [build_outbound(url)],
        "route": {"final": "proxy"},
    }


def install_sing_box(directory):
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        raise ProxyError("Automatic sing-box setup supports Linux x86_64 runners only.")
    name = f"sing-box-{SING_BOX_VERSION}-linux-amd64"
    url = f"https://github.com/SagerNet/sing-box/releases/download/v{SING_BOX_VERSION}/{name}.tar.gz"
    with urlopen(url, timeout=30) as response:
        archive = response.read(64 * 1024 * 1024 + 1)
    if hashlib.sha256(archive).hexdigest() != SING_BOX_SHA256:
        raise ProxyError("sing-box download checksum verification failed.")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
        with package.extractfile(f"{name}/sing-box") as source:
            binary = directory / "sing-box"
            binary.write_bytes(source.read())
    binary.chmod(0o700)
    return binary


def _runtime_directory(env):
    return Path(env.get("RUNNER_TEMP") or tempfile.gettempdir()) / "any-agent-checkin-proxy"


def _listener_open():
    try:
        with socket.create_connection((LISTEN_HOST, LISTEN_PORT), timeout=0.2):
            return True
    except OSError:
        return False


def start_proxy(env):
    url = env.get("PROXY_URL", "").strip()
    if not url:
        print("PROXY_URL is not configured; existing network settings are unchanged.")
        return
    config = build_config(url)
    if _listener_open():
        raise ProxyError("Local proxy port 8118 is already occupied.")
    directory = _runtime_directory(env)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path = directory / "config.json"
    with os.fdopen(os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as output:
        json.dump(config, output)
    binary = install_sing_box(directory)
    checked = subprocess.run([str(binary), "check", "-c", str(config_path)], capture_output=True, timeout=20)
    if checked.returncode:
        raise ProxyError("sing-box rejected the node configuration.")
    process = None
    try:
        with (directory / "sing-box.log").open("wb") as log:
            process = subprocess.Popen(
                [str(binary), "run", "-c", str(config_path)],
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
        (directory / "proxy.pid").write_text(str(process.pid), encoding="ascii")
        for _ in range(40):
            if process.poll() is not None:
                raise ProxyError("sing-box exited before its proxy listener became ready.")
            if _listener_open():
                break
            time.sleep(0.25)
        else:
            raise ProxyError("Timed out waiting for the local proxy listener.")
        if env.get("GITHUB_ENV"):
            with Path(env["GITHUB_ENV"]).open("a", encoding="utf-8") as output:
                output.write(f"PROXY_SERVER={PROXY_SERVER}\n")
    except Exception:
        if process is not None and process.poll() is None:
            process.terminate()
        (directory / "proxy.pid").unlink(missing_ok=True)
        raise
    print(f"Proxy ready: {config['outbounds'][0]['type']} via {PROXY_SERVER} (no direct fallback).")


def stop_proxy(env):
    directory = _runtime_directory(env)
    pid_file = directory / "proxy.pid"
    if pid_file.exists():
        pid = int(pid_file.read_text(encoding="ascii"))
        if pid <= 1:
            raise ProxyError("Invalid local proxy process identifier.")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        pid_file.unlink(missing_ok=True)
    for name in ("config.json", "sing-box.log"):
        (directory / name).unlink(missing_ok=True)
    print("Local proxy stopped; temporary node configuration removed.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "stop", "generate"))
    parser.add_argument("--output", type=Path, default=Path("config.json"))
    args = parser.parse_args(argv)
    try:
        if args.command == "start":
            start_proxy(os.environ)
        elif args.command == "stop":
            stop_proxy(os.environ)
        else:
            config = build_config(os.environ.get("PROXY_URL", "").strip())
            with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as output:
                json.dump(config, output, indent=2)
            print("Proxy configuration generated; node credentials are omitted from logs.")
    except ProxyError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print("Proxy operation failed; check the node and network configuration. Sensitive details are omitted.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
