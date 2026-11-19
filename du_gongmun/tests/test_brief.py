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
    "/main": """<a href="/gw" target="_blank">그룹웨어</a> <a href="#">학사</a>
                <a href="/gongram?p=1">공람대기</a>""",
    "/gongram?p=1": """<table><tr><th>제목</th><th>발신</th><th>일자</th></tr>
      <tr><td><a href="/doc1" target="_blank">2027 교육과정 개편 학과별 의견 제출 요청</a></td><td>교무처</td><td>2026-11-19</td></tr>
      <tr><td><a href="/doc2">청사 보수공사 안내</a></td><td>시설팀</td><td>2026-11-19</td></tr>
      <tr><td><a href="/doc3" target="_blank">창작지원 공모 안내</a></td><td>산학협력단</td><td>2026-11-18</td></tr>
      </table><a href="/gongram?p=1">1</a> <a href="/gongram?p=2">2</a>""",
    "/gongram?p=2": """<table><tr><td><a href="/doc4">회계 마감 안내</a></td><td>재무팀</td><td>26.11.18</td></tr>
      <tr><td><a href="/doc5">옛 공문</a></td><td>총무팀</td><td>2026-11-17</td></tr>
      </table><a href="/gongram?p=1">1</a> <a href="/gongram?p=2">2</a>""",
    "/doc1": "각 학과는 2026. 11. 25.까지 교육과정 의견을 제출하시기 바랍니다.",
    "/doc2": "본관 3층 공사로 엘리베이터 운행을 중지합니다.",
    "/doc3": "음악·공연 분야 창작자에게 제작비를 지원하는 공모입니다.",
    "/doc4": "부서 예산 집행 마감을 안내합니다.",
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


KW = dict(headless=True, debug=False, since=dt.date(2026, 11, 18), today=dt.date(2026, 11, 19), seen=set())


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
    pages = brief.collect(_cfg(portal), "secret", **KW)
    inbox, missing = pages
    assert "교육과정 개편안" in inbox["text"]
    assert any("연구윤리" in r for r in inbox["rows"])
    assert missing.get("error")


def test_bad_password_raises(portal):
    with pytest.raises(RuntimeError, match="로그인"):
        brief.collect(_cfg(portal), "wrong", **KW)


def test_format_and_chunk():
    items = [
        {"kind": "제출", "title": "교육과정 개편안 제출", "sender": "교무처", "date": "2026-11-17",
         "due": "2026-11-20", "summary": "학과별 교과목 변경안", "action": "11/20까지 개편안 제출", "urgent": True},
        {"kind": "관련", "title": "학사내규 개정 시행 통보", "sender": "", "date": "2026-11-19",
         "due": "", "summary": "", "action": "참고", "urgent": False},
    ]
    state = {"seen": [], "docs_sent": []}
    brief.mark_new(items, state)
    assert all(it["new"] for it in items)
    text = brief.format_brief({"headline": "개편안 마감 임박", "skipped": 12, "items": items},
                              dt.date(2026, 11, 18), ["일정"], intro="11/17(화) 이후 새 공람 14건 확인")
    assert "📝 해야 할 것 (제출·회신·신청) (1)" in text
    assert "🔴 교육과정 개편안 제출 ~11/20(금) [교무처]" in text
    assert "   학과별 교과목 변경안" in text and "   → 11/20까지 개편안 제출" in text
    assert "📌 알아야 할 공문 (1)" in text and "일정" not in text.replace("못 읽은 화면: 일정", "")
    assert "학과와 관련 없는 12건은 뺐습니다" in text
    assert "⚠️ 못 읽은 화면: 일정" in text
    parts = kakao.chunk(text * 5)
    assert all(len(p) <= 200 for p in parts) and len(parts) > 1


def test_since_date():
    today = dt.date(2026, 11, 23)  # 월요일
    assert brief.since_date({}, today) == dt.date(2026, 11, 22)
    assert brief.since_date({"last_day": "2026-11-20"}, today) == dt.date(2026, 11, 20)  # 금요일부터
    assert brief.since_date({"last_day": "2026-11-23"}, today) == dt.date(2026, 11, 22)  # 오늘 또 돌리면 어제부터
    assert brief.since_date({"last_day": "2026-10-01"}, today) == dt.date(2026, 11, 16)  # 최대 7일


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
    assert todo["kind"] == "제출" and todo["due"] == "2026-11-20" and todo["urgent"]
    assert by["연구윤리 교육 이수 안내"]["kind"] == "관련"
    assert "지난 학기 안내" not in by and "제목 | 발신" not in by
    assert {it["kind"] for it in out["items"]} <= {"제출", "관련"}


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
    out = brief.format_brief({"headline": "", "skipped": 0, "items": []}, dt.date(2026, 11, 19), [], ["결재대기 2건"])
    assert out.splitlines()[1] == "📊 결재대기 2건"


def test_wait_counts_waits_for_loading(monkeypatch):
    texts = iter(["데이터 조회중입니다", "결재대기 0건", "결재대기 3건", "결재대기 3건", "결재대기 3건"])
    monkeypatch.setattr(brief, "page_text", lambda page: next(texts))

    class P:
        def wait_for_timeout(self, ms):
            pass
    brief.wait_counts(P(), ["결재대기"], 30)
    assert next(texts) == "결재대기 3건"  # 두 번 연속 3건을 확인하고 멈춤


def test_rules_skip_past_year_titles():
    rows = ["2019학년도 하반기 교원 단기연수 참가", "2025 한국전문대학교육협의회 온라인 연수 참가",
            "[20250082] [25-12]2027학년도 전문기술석사과정 설치 인가 신청서 작성",
            "2026 한국전문대학교육협의회 온라인 연수 참가"]
    out = summarize.with_rules([{"name": "그룹웨어", "url": "", "text": "", "rows": rows}],
                               dt.date(2026, 11, 19))
    assert [it["title"][:12] for it in out["items"]] == ["[20250082] [", "2026 한국전문대학교"]


def test_gongram_list_since_yesterday_with_bodies(portal):
    cfg = _cfg(portal)
    cfg["pages"] = [{"name": "공람대기", "click": ["공람대기"], "list": True}]
    (res,) = brief.collect(cfg, "secret", **KW)
    titles = [d["title"] for d in res["docs"]]
    assert titles == ["2027 교육과정 개편 학과별 의견 제출 요청", "청사 보수공사 안내",
                      "창작지원 공모 안내", "회계 마감 안내"]  # 11/17 은 제외, 2쪽까지 넘어감
    bodies = {d["title"]: d["body"] for d in res["docs"]}
    assert "교육과정 의견" in bodies["2027 교육과정 개편 학과별 의견 제출 요청"]  # 새 창
    assert "엘리베이터" in bodies["청사 보수공사 안내"]  # 같은 창 이동 후 목록으로 복귀
    assert "예산 집행" in bodies["회계 마감 안내"]  # 2쪽 공문

    out = summarize.with_rules([res], dt.date(2026, 11, 19), {"department": "실용음악학과"})
    assert out["skipped"] == 2
    (todo,) = [it for it in out["items"] if it["kind"] == "제출"]
    assert todo["due"] == "2026-11-25"
    assert [it["title"] for it in out["items"] if it["kind"] == "관련"] == ["창작지원 공모 안내"]

    # 이미 보낸 공문은 다음날 다시 안 나옴
    seen = {res["docs"][0]["key"]}
    (res2,) = brief.collect(cfg, "secret", **{**KW, "seen": seen})
    assert len(res2["docs"]) == 3


def test_row_date_formats():
    t = dt.date(2026, 11, 19)
    assert summarize.row_date(["제목", "2026.11.18"], t) == dt.date(2026, 11, 18)
    assert summarize.row_date(["제목", "26-11-17"], t) == dt.date(2026, 11, 17)
    assert summarize.row_date(["제목", "11-18"], t) == dt.date(2026, 11, 18)
    assert summarize.row_date(["제목", "12-30"], t) == dt.date(2025, 12, 30)
    assert summarize.row_date(["제목", "09:41"], t) == t
    assert summarize.row_date(["제목", "교무처"], t) is None


def test_rules_keep_general_department_topics():
    docs = [
        {"title": "2026학년도 졸업작품 전시 및 캡스톤디자인 결과보고서 제출", "date": "2026-11-18",
         "body": "각 학과는 12. 5.까지 결과보고서를 제출", "key": "a", "line": ""},
        {"title": "취업 특강 개최 안내", "date": "2026-11-18", "body": "재학생 대상 특강", "key": "b", "line": ""},
        {"title": "법인카드 사용 지침", "date": "2026-11-18", "body": "경리 담당자 참고", "key": "c", "line": ""},
    ]
    out = summarize.with_rules([{"name": "공람대기", "docs": docs}], dt.date(2026, 11, 19), {})
    kinds = {it["title"].split()[0]: it["kind"] for it in out["items"]}
    assert kinds == {"2026학년도": "제출", "취업": "관련"}
    assert out["skipped"] == 1
    assert "졸업" in summarize.system_prompt({}) and "캡스톤" in summarize.system_prompt({})


def test_rules_keep_meetings_and_guidelines():
    docs = [
        {"title": "2026-2학기 전체 교수회의 개최 안내", "date": "2026-11-18", "body": "11. 26.(목) 14시 대강당",
         "key": "m", "line": ""},
        {"title": "교수학습 지침 개발 협조 요청", "date": "2026-11-18", "body": "학과별 의견 회신",
         "key": "g", "line": ""},
    ]
    out = summarize.with_rules([{"name": "공람대기", "docs": docs}], dt.date(2026, 11, 19), {})
    kinds = {it["title"]: it["kind"] for it in out["items"]}
    assert kinds == {"2026-2학기 전체 교수회의 개최 안내": "관련", "교수학습 지침 개발 협조 요청": "제출"}
    assert out["skipped"] == 0


def test_relative_dates_and_real_list_titles():
    t = dt.date(2026, 11, 19)
    assert summarize.row_date(["일반공문", "제목", "김준현", "16시간 전"], t) == t
    assert summarize.row_date(["일반공문", "제목", "김준현", "2일 전"], t) == dt.date(2026, 11, 17)
    titles = ["2027학년도 시설분야 예산편성 의견 수렴서 제출 요청",
              "2026학년도 실험실습 기자재 및 소모품 잔여 예산에 대한 구입요구서 제출 요청",
              "대중문화사업단 2026 대중음악 비즈니스 인력양성 지원사업 프로그램 출석인정 협조 요청 출석인정 대상자 명단",
              "직원 인사발령 통보"]
    docs = [{"title": x, "date": "2026-11-18", "body": "", "key": str(i), "line": ""} for i, x in enumerate(titles)]
    out = summarize.with_rules([{"name": "공람대기", "docs": docs}], t, {})
    kinds = {it["title"][:10]: it["kind"] for it in out["items"]}
    assert kinds["2027학년도 시설"] == "제출" and kinds["2026학년도 실험"] == "제출"
    assert kinds["대중문화사업단 20"] == "제출"
    assert out["skipped"] == 1  # 직원 인사발령


def test_then_url_inside_groupware_popup(portal):
    cfg = _cfg(portal)
    cfg["pages"] = [{"name": "공람대기", "click": ["그룹웨어"], "then_url": f"{portal}/gongram?p=1",
                     "list": True, "open_items": False}]
    (res,) = brief.collect(cfg, "secret", **KW)
    assert len(res["docs"]) == 4 and not res.get("error")


def test_range_deadline_and_snippet_skip_attachments():
    t = dt.date(2026, 11, 19)
    body = ("붙임-2026-2학기 성적 평점표 작성 및 제출 안내.pdf\n"
            "1. 관련: 학사운영팀\n"
            "2. 성적입력 기간: 2026. 11. 23.(월) ~ 12. 4.(금) 18:00\n")
    assert summarize._due(body, t) == dt.date(2026, 12, 4)
    assert summarize._snippet(body, ("성적",)).startswith("2. 성적입력 기간")
    assert summarize._due("신청기간: 11. 20.(금) ~ 27.(금)", t) == dt.date(2026, 11, 27)
    assert summarize._snippet("27년 1학기 신청 포스터.jpg", ("신청",)) == ""
