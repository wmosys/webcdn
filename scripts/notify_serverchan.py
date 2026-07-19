#!/usr/bin/env python3
"""向 Server 酱 3 推送 Markdown 消息。

使用方式：
    SERVER_CHAN_SEND_URL=<url> python3 scripts/notify_serverchan.py <title> <report_file>

推送 URL 内含 sendkey，属于敏感凭据，只从环境变量 SERVER_CHAN_SEND_URL 读取，
避免作为命令行参数在进程列表或日志中泄露。

用 JSON body 提交，避免表单 urlencode 对 desp 长度的限制导致消息被截断。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request


def main() -> int:
    if len(sys.argv) != 3:
        print("用法: notify_serverchan.py <title> <report_file>", file=sys.stderr)
        return 2

    url = os.environ.get("SERVER_CHAN_SEND_URL", "").strip()
    if not url:
        print("环境变量 SERVER_CHAN_SEND_URL 未设置。", file=sys.stderr)
        return 2

    title, report_file = sys.argv[1:3]

    with open(report_file, encoding="utf-8") as fh:
        desp = fh.read()

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
            body = resp.read().decode("utf-8", errors="replace")[:200]
            print(f"{resp.status} {body}")
            return 0
    except Exception as exc:  # noqa: BLE001
        print(f"通知发送失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
