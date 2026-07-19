#!/usr/bin/env python3
"""向 Server 酱 3 推送 Markdown 消息。

使用方式：
    python3 scripts/notify_serverchan.py <url> <title> <report_file>

用 JSON body 提交，避免表单 urlencode 对 desp 长度的限制导致消息被截断。
"""

from __future__ import annotations

import json
import sys
import urllib.request


def response_error(body: str) -> str | None:
    """检查 Server 酱返回体，返回错误描述；成功返回 None。

    Server 酱即使调用失败（如 sendkey 无效）也返回 HTTP 200，
    真正的成败在响应 JSON 的 `code` 字段（0 表示成功）。
    仅按 HTTP 状态判断会把逻辑失败误判成成功。
    """

    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        # 无法解析响应体：无法确认成功，视为失败以便向上传播。
        return "响应体不是合法 JSON，无法确认推送结果"
    if not isinstance(data, dict):
        return "响应体不是 JSON 对象，无法确认推送结果"
    code = data.get("code")
    if code in (0, None):
        return None
    message = data.get("message") or data.get("msg") or ""
    return f"code={code} {message}".strip()


def main() -> int:
    if len(sys.argv) != 4:
        print("用法: notify_serverchan.py <url> <title> <report_file>", file=sys.stderr)
        return 2

    url, title, report_file = sys.argv[1:4]

    try:
        with open(report_file, encoding="utf-8") as fh:
            desp = fh.read()
    except OSError as exc:
        print(f"读取报告文件失败：{exc}", file=sys.stderr)
        return 1

    payload = json.dumps(
        {"title": title, "desp": desp, "tags": "Github Actions"},
        ensure_ascii=False,
    ).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        print(f"通知发送失败：{exc}", file=sys.stderr)
        return 1

    error = response_error(body)
    if error is not None:
        print(f"通知发送失败：{error}｜响应：{body[:200]}", file=sys.stderr)
        return 1
    print(f"通知发送成功：{body[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
