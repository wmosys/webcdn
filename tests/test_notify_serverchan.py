"""`scripts/notify_serverchan.py` 的单元测试。

对参数校验、请求构造（JSON body、header、method）与成功/失败返回码做
覆盖，网络调用通过 mock `urllib.request.urlopen` 隔离。
"""

from __future__ import annotations

import json

import pytest

import notify_serverchan as ns


class _FakeResp:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def test_wrong_arg_count_returns_2(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["notify_serverchan.py", "only-one"])
    assert ns.main() == 2
    err = capsys.readouterr().err
    assert "用法" in err


def test_success_builds_json_request(monkeypatch, tmp_path, capsys):
    report = tmp_path / "report.md"
    report.write_text("# 报告\n内容", encoding="utf-8")

    captured = {}

    def fake_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["headers"] = req.headers
        captured["method"] = req.get_method()
        return _FakeResp(200, b"ok")

    monkeypatch.setattr(ns.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        "sys.argv",
        ["notify_serverchan.py", "https://push.example/send", "标题", str(report)],
    )

    assert ns.main() == 0

    payload = json.loads(captured["data"].decode("utf-8"))
    assert payload == {"title": "标题", "desp": "# 报告\n内容", "tags": "Github Actions"}
    assert captured["url"] == "https://push.example/send"
    assert captured["method"] == "POST"
    # header 名称在 urllib 中会被首字母大写化
    assert captured["headers"]["Content-type"] == "application/json"

    out = capsys.readouterr().out
    assert "200" in out


def test_request_failure_returns_1(monkeypatch, tmp_path, capsys):
    report = tmp_path / "report.md"
    report.write_text("body", encoding="utf-8")

    def boom(req, timeout=30):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(ns.urllib.request, "urlopen", boom)
    monkeypatch.setattr(
        "sys.argv",
        ["notify_serverchan.py", "https://x", "t", str(report)],
    )

    assert ns.main() == 1
    err = capsys.readouterr().err
    assert "通知发送失败" in err


def test_missing_report_file_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "sys.argv",
        ["notify_serverchan.py", "https://x", "t", str(tmp_path / "nope.md")],
    )
    with pytest.raises(FileNotFoundError):
        ns.main()
