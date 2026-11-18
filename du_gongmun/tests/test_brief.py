"""가짜 포털(로그인 → 팝업 그룹웨어 → iframe 안 수신함)로 수집 흐름과 메시지 포맷을 검사."""
import datetime as dt
import os
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import brief  # noqa: E402
import kakao  # noqa: E402

PAGES = {
    "/login": """<form method=post action=/login><input type=text name=id>
                 <input type=password name=pw><button>로그인</button></form>""",
    "/main": """<a href="/gw" target="_blank">그룹웨어</a> <a href="#">학사</a>""",
    "/gw": """<a href="/approval">전자결재</a>""",
    "/approval": """<iframe src="/menu" width=800 height=600></iframe>""",
    "/menu": """<a href="/inbox">수신함</a>""",
    "/inbox": """<table><tr><th>제목</th><th>발신</th><th>일자</th></tr>
                 <tr><td>2027학년도 교육과정 개편안 제출 요청</td><td>교무처</td><td>2026-11-17</td></tr>
                 <tr><td>연구윤리 교육 이수 안내</td><td>산학협력단</td><td>2026-11-16</td></tr></table>""",
}


class Handler(BaseHTTPRequestHandler):
    def _html(self, body, code=200, headers=()):
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"<html><body>{body}</body></html>".encode())

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(n).decode()
        if "id=prof" in data and "pw=secret" in data:
            self._html("", 302, [("Location", "/main"), ("Set-Cookie", "s=1; Path=/")])
        else:
            self._html(PAGES["/login"] + "<p>실패</p>")

    def do_GET(self):  # noqa: N802
        if self.path != "/login" and "s=1" not in (self.headers.get("Cookie") or ""):
            return self._html("", 302, [("Location", "/login")])
        self._html(PAGES.get(self.path, "not found"))

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def portal():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _cfg(base):
    cfg = {"login_url": f"{base}/login", "username": "prof",
           "pages": [{"name": "수신함", "click": ["그룹웨어", "전자결재", "수신함"]},
                     {"name": "없는메뉴", "click": ["없음"]}]}
    if os.path.exists("/opt/pw-browsers/chromium"):
        cfg["browser_path"] = "/opt/pw-browsers/chromium"
    return cfg


def test_collect_follows_popup_and_iframe(portal):
    pages = brief.collect(_cfg(portal), "secret", headless=True, debug=False)
    inbox, missing = pages
    assert "교육과정 개편안" in inbox["text"]
    assert any("연구윤리" in r for r in inbox["rows"])
    assert missing.get("error")


def test_bad_password_raises(portal):
    with pytest.raises(RuntimeError, match="로그인"):
        brief.collect(_cfg(portal), "wrong", headless=True, debug=False)


def test_format_and_chunk(tmp_path, monkeypatch):
    monkeypatch.setattr(brief, "STATE_FILE", tmp_path / "s.json")
    items = [
        {"kind": "할일", "title": "교육과정 개편안 제출", "sender": "교무처", "date": "2026-11-17",
         "due": "2026-11-20", "action": "11/20까지 개편안 제출", "urgent": True},
        {"kind": "일정", "title": "학과 회의", "sender": "", "date": "2026-11-19",
         "due": "", "action": "참고", "urgent": False},
    ]
    brief.mark_new(items)
    assert all(it["new"] for it in items)
    brief.mark_new(items)
    assert not any(it["new"] for it in items)
    text = brief.format_brief({"headline": "개편안 마감 임박", "items": items}, dt.date(2026, 11, 18), ["일정"])
    assert "🔴 교육과정 개편안 제출 ~11/20(금)" in text
    assert "📅 일정 (1)" in text and "⚠️ 못 읽은 화면: 일정" in text
    parts = kakao.chunk(text * 5)
    assert all(len(p) <= 200 for p in parts) and len(parts) > 1


import summarize  # noqa: E402


def test_rules_summary_from_table_rows():
    pages = [{"name": "공문 수신함", "url": "", "text": "", "rows": [
        "2027학년도 교육과정 개편안 제출 요청 (11/20까지) | 교무처 | 2026-11-17",
        "연구윤리 교육 이수 안내 | 산학협력단 | 2026-11-16",
        "지난 학기 안내 | 학생처 | 2026-08-01",
        "제목 | 발신",
    ]}, {"name": "일정", "url": "", "text": "2026.11.19 학과 회의\n2026.10.01 지난 회의", "rows": []}]
    out = summarize.with_rules(pages, dt.date(2026, 11, 18))
    by = {it["title"]: it for it in out["items"]}
    todo = by["2027학년도 교육과정 개편안 제출 요청 (11/20까지)"]
    assert todo["kind"] == "할일" and todo["due"] == "2026-11-20" and todo["urgent"]
    assert by["연구윤리 교육 이수 안내"]["kind"] == "공문"
    assert "지난 학기 안내" not in by and "제목 | 발신" not in by
    assert [it["title"] for it in out["items"] if it["kind"] == "일정"] == ["2026.11.19 학과 회의"]


def test_claude_cli_output_parsed(tmp_path):
    import json
    inner = {"headline": "h", "items": [{"kind": "할일", "title": "t", "due": "2026-11-20"}]}
    (tmp_path / "out.json").write_text(json.dumps(
        {"type": "result", "is_error": False, "result": "```json\n" + json.dumps(inner) + "\n```"}))
    fake = tmp_path / "claude"
    fake.write_text(f"#!/bin/sh\ncat >/dev/null\ncat '{tmp_path / 'out.json'}'\n")
    fake.chmod(0o755)
    pages = [{"name": "x", "url": "", "text": "t", "rows": []}]
    out, how = summarize.summarize(pages, dt.date(2026, 11, 18), {"claude_path": str(fake)}, None)
    assert how == "Claude(구독)"
    assert out["items"][0]["due"] == "2026-11-20" and out["items"][0]["urgent"] is False


def test_falls_back_to_rules_when_no_cli(monkeypatch):
    monkeypatch.setattr(summarize, "find_claude_cli", lambda: None)
    _, how = summarize.summarize([{"name": "x", "url": "", "text": "", "rows": []}],
                                 dt.date(2026, 11, 18), {}, None)
    assert how == "규칙"


def test_deliver_reports_failures(monkeypatch):
    import notify
    sent = []
    monkeypatch.setattr(notify, "mac", lambda text, f, o=True: sent.append("mac"))
    failed = notify.deliver("제목\n내용", {"delivery": ["mac", "imessage"]}, None)
    assert sent == ["mac"] and failed == ["imessage"]  # imessage_to 가 없으니 실패


def test_email_sends_to_self(monkeypatch):
    import notify
    log = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            log["host"] = (host, port)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def login(self, user, pw):
            log["login"] = (user, pw)
        def send_message(self, msg):
            log["msg"] = msg

    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(notify.keyring, "get_password", lambda s, k: "abcd efgh ijkl mnop")
    notify.email("📋 브리핑\n내용", "me@example.com")
    assert log["host"] == ("smtp.gmail.com", 465)
    assert log["login"] == ("me@example.com", "abcdefghijklmnop")
    assert log["msg"]["To"] == "me@example.com" and "브리핑" in log["msg"]["Subject"]


def test_dashboard_counts():
    text = "나의현황(교원용)\n메일\n0건\n결재대기\n2건\n수/발신대기\n1 / 0 건\n공람대기\n0건"
    assert brief.extract_counts(text, ["결재대기", "수/발신대기", "공람대기", "없는것"]) == [
        "결재대기 2건", "수/발신대기 1/0건", "공람대기 0건"]
    out = brief.format_brief({"headline": "", "items": []}, dt.date(2026, 11, 19), [], ["결재대기 2건"])
    assert out.splitlines()[1] == "📊 결재대기 2건"


def test_wait_counts_waits_for_loading(monkeypatch):
    texts = iter(["데이터 조회중입니다", "결재대기 0건", "결재대기 3건", "결재대기 3건", "결재대기 3건"])
    monkeypatch.setattr(brief, "page_text", lambda page: next(texts))

    class P:
        def wait_for_timeout(self, ms):
            pass
    brief.wait_counts(P(), ["결재대기"], 30)
    assert next(texts) == "결재대기 3건"  # 두 번 연속 3건을 확인하고 멈춤
