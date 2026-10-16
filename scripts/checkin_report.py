"""Publish safe service-level results to README and Telegram."""

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Mapping
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


START_MARKER = b"<!-- checkin-results:start -->"
END_MARKER = b"<!-- checkin-results:end -->"
BEIJING = timezone(timedelta(hours=8))
STATUS_LABELS = {
    "success": "成功",
    "failure": "失败",
    "cancelled": "已取消",
    "skipped": "已跳过",
    "unknown": "未知",
}
HTTP_TIMEOUT = 20
MAX_RESPONSE_BYTES = 65536


class ReportError(Exception):
    """An error whose message is safe to print in public workflow logs."""


def normalize_status(value: str) -> str:
    status = value.strip().lower()
    return status if status in STATUS_LABELS else "unknown"


@dataclass(frozen=True)
class CheckinReport:
    agent_router: str
    anyrouter: str
    beijing_time: str
    run_url: str

    def service_results(self) -> tuple[tuple[str, str], ...]:
        return (("Agent Router", self.agent_router), ("AnyRouter", self.anyrouter))

    def telegram_text(self) -> str:
        lines = ["每日签到结果"]
        lines.extend(
            f"{service}：{STATUS_LABELS[status]} ({status})"
            for service, status in self.service_results()
        )
        lines.extend((f"北京时间：{self.beijing_time}", f"运行记录：{self.run_url}"))
        return "\n".join(lines)

    def markdown(self) -> str:
        lines = [f"北京时间：{self.beijing_time}", "", "| 服务 | 状态 |", "| --- | --- |"]
        lines.extend(
            f"| {service} | {STATUS_LABELS[status]} (`{status}`) |"
            for service, status in self.service_results()
        )
        lines.extend(("", f"[查看本次运行]({self.run_url})"))
        return "\n".join(lines)


def load_report(env: Mapping[str, str]) -> CheckinReport:
    server = env.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
    parsed_server = urlsplit(server)
    repository = env.get("GITHUB_REPOSITORY", "")
    run_id = env.get("GITHUB_RUN_ID", "")
    if (
        parsed_server.scheme != "https"
        or not parsed_server.hostname
        or parsed_server.username is not None
        or parsed_server.password is not None
        or parsed_server.path
        or parsed_server.query
        or parsed_server.fragment
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or not re.fullmatch(r"[0-9]+", run_id)
    ):
        raise ReportError("Invalid GitHub run metadata.")

    raw_time = env.get("REPORT_TIME", "")
    if raw_time:
        try:
            report_time = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        except ValueError:
            raise ReportError("REPORT_TIME must be an ISO 8601 timestamp.") from None
        if report_time.tzinfo is None:
            raise ReportError("REPORT_TIME must include a timezone.")
    else:
        report_time = datetime.now(timezone.utc)

    return CheckinReport(
        normalize_status(env.get("AGENT_ROUTER_RESULT", "")),
        normalize_status(env.get("ANYROUTER_RESULT", "")),
        report_time.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S (UTC+8)"),
        f"{server}/{repository}/actions/runs/{run_id}",
    )


def replace_results(content: bytes, report: CheckinReport) -> bytes:
    if content.count(START_MARKER) != 1 or content.count(END_MARKER) != 1:
        raise ReportError("README must contain exactly one pair of check-in result markers.")
    start = content.index(START_MARKER) + len(START_MARKER)
    end = content.index(END_MARKER)
    if start > end:
        raise ReportError("README check-in result markers are out of order.")
    newline = b"\r\n" if b"\r\n" in content else b"\n"
    block = report.markdown().encode("utf-8").replace(b"\n", newline)
    return content[:start] + newline + newline + block + newline + newline + content[end:]


def update_readme(path: Path, report: CheckinReport) -> None:
    content = path.read_bytes()
    updated = replace_results(content, report)
    if updated != content:
        path.write_bytes(updated)
    print("README check-in results updated.")


def send_telegram(report: CheckinReport, token: str, chat_id: str) -> bool:
    token, chat_id = token.strip(), chat_id.strip()
    if not token or not chat_id:
        print("Telegram notification skipped: configure both TG_BOT_TOKEN and TG_CHAT_ID.")
        return False

    # Bot URLs contain a credential. Never log raw exceptions or API response bodies.
    try:
        request = Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps(
                {"chat_id": chat_id, "text": report.telegram_text(), "disable_web_page_preview": True},
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urlopen(request, timeout=HTTP_TIMEOUT) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ReportError("Telegram notification failed: response too large.")
        result = json.loads(body)
    except HTTPError as error:
        status = error.code
        error.close()
        raise ReportError(f"Telegram notification failed: HTTP {status}.") from None
    except ReportError:
        raise
    except Exception:
        raise ReportError("Telegram notification failed: network or invalid response error.") from None

    if not isinstance(result, dict) or result.get("ok") is not True:
        raise ReportError("Telegram notification failed: API rejected the message.")
    print("Combined Telegram notification sent.")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    update = commands.add_parser("update-readme", help="Replace only the marked README results block.")
    update.add_argument("--readme", type=Path, default=Path("README.md"))
    commands.add_parser("notify", help="Send one combined Telegram message, if configured.")
    args = parser.parse_args(argv)

    try:
        report = load_report(os.environ)
        if args.command == "update-readme":
            update_readme(args.readme, report)
        else:
            send_telegram(report, os.environ.get("TG_BOT_TOKEN", ""), os.environ.get("TG_CHAT_ID", ""))
    except ReportError as error:
        print(str(error), file=sys.stderr)
        return 1
    except OSError:
        print("Unable to update README: file access failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
