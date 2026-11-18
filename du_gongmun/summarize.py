"""그룹웨어 공람 공문 → 우리 학과가 해야 할 것 / 알아야 할 것 요약.

세 가지 방식 (config.yaml 의 summarizer):
  auto       맥에 Claude Code(claude 명령)가 있으면 그걸, 없으면 rules  ← 기본값, 무료
  claude_cli 맥에 로그인된 Claude Code 사용 (Claude 구독에 포함, 추가 요금 없음)
  rules      AI 없이 날짜·'제출/회신/까지' 같은 단어로 정리 (완전 무료, 인터넷 외부 전송 없음)
  api        Claude API 키 사용 (유료, 사용량만큼 과금)
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

log = logging.getLogger("brief")

SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "skipped": {"type": "integer"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["제출", "관련"]},
                    "title": {"type": "string"},
                    "sender": {"type": "string"},
                    "date": {"type": "string"},
                    "due": {"type": "string"},
                    "summary": {"type": "string"},
                    "action": {"type": "string"},
                    "urgent": {"type": "boolean"},
                },
                "required": ["kind", "title", "sender", "date", "due", "summary", "action", "urgent"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["headline", "skipped", "items"],
    "additionalProperties": False,
}

SYSTEM_TMPL = """당신은 동서울대학교 {dept} 교수의 아침 업무 비서입니다.
학교 그룹웨어 공람함에 새로 들어온 공문(제목·목록 정보·본문)을 받아, 우리 학과에 필요한 것만 골라 정리합니다.

우리 학과: {dept}. 관련 분야: {keywords}.
예술 분야가 아니어도 모든 학과·전체 교원에 해당하는 공문은 반드시 남깁니다
(전체 교수회의·교직원 회의, 교수학습 지침 개발, 규정 개정, 설문·의견수렴 등 포함):
{topics} 등.

남길 것:
- "제출": 우리 학과나 교수가 제출·회신·신청·입력·작성·참석 등 직접 해야 하는 일이 있는 공문.
  '각 학과', '전 학과', '학과장', '전체 교원', '전 교원' 대상이면 우리 학과도 해당합니다.
- "관련": 해야 할 일은 없지만 우리 학과가 알아야 할 공문(전 학과 대상 학사·졸업·특강·캡스톤·취업·장학 안내,
  예술·음악·공연 관련 공모·지원사업·행사, 학생 관련 안내 등).
  참석·신청이 필요한 회의·연수·교육도 해야 할 일이 있으면 "제출", 아니면 "관련"으로 넣습니다.
공람함은 교수 본인 앞으로 온 공문이므로 기본은 남기고, 아래만 뺍니다.
뺄 것: 직원 인사발령·직제개편, 모금·성금 협조, 특정 다른 학과·부서만 해당하는 공문, 행정 부서끼리의 내부 공문, 단순 결과 통보, 우리와 무관한 외부 기관 홍보.
뺀 공문 수는 skipped 에 넣으세요. 애매하면 "관련"으로 남기세요.

각 항목:
- title: 공문 제목을 짧게(문서번호 같은 대괄호 번호는 빼도 됨). sender: 발신 부서(모르면 빈 문자열).
- date: 접수/등록일, due: 제출·회신 마감일. 둘 다 YYYY-MM-DD, 모르면 빈 문자열.
- summary: 우리 학과 입장에서 핵심 내용 한두 문장(무엇을, 누구 대상, 얼마).
- action: 해야 할 행동 한 문장(예: "11/25까지 학과 교육과정 변경안을 교무처에 제출"). 없으면 "참고".
- urgent: 마감이 3일 이내이거나 지났으면 true.
- 공문에 없는 내용은 지어내지 마세요.
headline: 오늘 가장 먼저 챙길 것 한 줄."""


def system_prompt(cfg: dict) -> str:
    return SYSTEM_TMPL.format(dept=cfg.get("department", "실용음악학과"),
                              keywords=", ".join(cfg.get("keywords") or DEFAULT_KEYWORDS),
                              topics=", ".join(cfg.get("topics") or DEFAULT_TOPICS))

WEEKDAYS = "월화수목금토일"
DEFAULT_KEYWORDS = ["실용음악", "음악", "예술", "공연", "보컬", "작곡", "연주", "실기", "콘서트",
                    "음원", "저작권", "오디션", "문화", "콘텐츠"]
# 예술 분야가 아니어도 학과 운영에 필요한 주제 (config.yaml 의 topics 로 바꿀 수 있음)
DEFAULT_TOPICS = ["졸업", "특강", "캡스톤", "교육과정", "교과목", "수업", "강의", "시간표", "성적", "출석",
                  "학사", "휴학", "복학", "학생지도", "상담", "취업", "현장실습", "산학", "인턴", "장학",
                  "입시", "신입생", "모집", "전공", "학과", "교원", "교수", "연수", "강의평가", "비교과",
                  "역량", "포트폴리오", "공모전", "경진대회", "LINC", "RISE", "혁신지원", "학습",
                  "학생", "행사", "축제", "평가", "인증", "실험실습", "기자재", "교수학습", "학습지침", "강의지침", "교육지침", "지침 개발", "회의", "전체회의", "교수회의", "교직원", "워크숍", "설명회", "간담회", "위원회", "세미나", "설문", "의견수렴", "학칙"]
# 공람함은 내 앞으로 온 공문이라 기본은 모두 남기고, 아래처럼 학과 업무와 무관한 종류만 뺀다
DEFAULT_SKIP = ["인사발령", "직제개편", "모금", "성금", "헌혈", "부고", "조의", "홍보비 집행",
                "법인카드", "회계 마감", "계약 체결", "청사", "보수공사", "주차"]
ALL_DEPT_WORDS = ("각 학과", "전 학과", "전학과", "학과장", "학과별", "전체 교원", "전 교원", "전교원",
                  "전임교원", "각 학부", "학부(과)", "학과(부)")


def _prompt_body(pages: list[dict], today: dt.date) -> str:
    parts = []
    for p in pages:
        if p.get("counts_only"):
            continue
        if "docs" in p:
            if not p["docs"]:
                parts.append(f"=== {p['name']}: 새 공문 없음 ===")
            for i, d in enumerate(p["docs"], 1):
                parts.append(f"=== {p['name']} 공문 {i} ===\n목록: {d['line']}\n본문:\n{d['body'] or '(본문 못 읽음)'}")
        else:
            parts.append(f"=== 화면: {p['name']} ({p['url']}) ===\n"
                         f"{p['text'] or '(읽기 실패: ' + p.get('error', '') + ')'}")
    return f"오늘은 {today.isoformat()} ({WEEKDAYS[today.weekday()]}요일)입니다.\n\n" + "\n\n".join(parts)


KIND_ALIASES = {"할일": "제출", "공문": "관련", "일정": "관련"}


def _valid(data: dict) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("요약 결과 형식이 올바르지 않습니다.")
    keys = SCHEMA["properties"]["items"]["items"]["required"]
    items = []
    for it in data["items"]:
        if not isinstance(it, dict) or not it.get("title"):
            continue
        it = {k: it.get(k, False if k == "urgent" else "") for k in keys}
        it["kind"] = KIND_ALIASES.get(it["kind"], it["kind"])
        if it["kind"] not in ("제출", "관련"):
            it["kind"] = "관련"
        it["urgent"] = bool(it["urgent"])
        items.append(it)
    try:
        skipped = int(data.get("skipped") or 0)
    except (TypeError, ValueError):
        skipped = 0
    return {"headline": str(data.get("headline", "")), "skipped": skipped, "items": items}


# ───────── Claude Code (구독, 무료 추가비용) ─────────
CLI_PATHS = ["~/.local/bin/claude", "~/.claude/local/claude", "/opt/homebrew/bin/claude",
             "/usr/local/bin/claude", "~/.npm-global/bin/claude"]


def find_claude_cli() -> str | None:
    found = shutil.which("claude")  # launchd 에서는 PATH 가 짧아서 직접도 찾아본다
    if found:
        return found
    for p in CLI_PATHS:
        p = os.path.expanduser(p)
        if os.access(p, os.X_OK):
            return p
    return None


def _extract_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("JSON을 찾지 못했습니다.")
    return json.loads(text[start:end + 1])


def with_claude_cli(pages: list[dict], today: dt.date, cli: str, cfg: dict) -> dict:
    prompt = (
        system_prompt(cfg)
        + "\n\n아래 JSON 형식으로만 답하세요. 설명이나 코드블록 없이 JSON 하나만 출력합니다.\n"
        + json.dumps(SCHEMA, ensure_ascii=False)
        + "\n\n"
        + _prompt_body(pages, today)
    )
    with tempfile.TemporaryDirectory() as empty:  # 빈 폴더에서 실행 (파일 접근할 것 없음)
        r = subprocess.run([cli, "-p", "--output-format", "json"], input=prompt, text=True,
                           capture_output=True, cwd=empty, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"claude 실행 실패: {(r.stderr or r.stdout)[:300]}")
    out = json.loads(r.stdout)
    if out.get("is_error"):
        raise RuntimeError(f"claude 오류: {str(out.get('result'))[:300]}")
    return _valid(_extract_json(out.get("result", "")))


# ───────── Claude API (유료) ─────────
def with_api(pages: list[dict], today: dt.date, api_key: str, model: str, cfg: dict) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=system_prompt(cfg),
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": _prompt_body(pages, today)}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude가 요약을 거절했습니다.")
    return _valid(json.loads(next(b.text for b in resp.content if b.type == "text")))


# ───────── 규칙 기반 (완전 무료) ─────────
FULL_DATE = re.compile(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")
DUE_FULL = re.compile(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*[일.]?\s*(?:\([^)]{1,3}\))?\s*(?:\d{1,2}:\d{2}\s*)?까지")
DUE_SHORT = re.compile(r"(?<![\d.])(\d{1,2})\s*[./월]\s*(\d{1,2})\s*[일.]?\s*(?:\([^)]{1,3}\))?\s*(?:\d{1,2}:\d{2}\s*)?까지")
YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
TODO_WORDS = ("제출", "회신", "요청", "마감", "기한", "까지", "결재", "검토", "입력", "신청", "작성", "조사", "협조")



def _date(y, m, d) -> dt.date | None:
    try:
        return dt.date(int(y), int(m), int(d))
    except ValueError:
        return None


def _due(line: str, today: dt.date) -> dt.date | None:
    m = DUE_FULL.search(line)
    if m:
        return _date(*m.groups())
    m = DUE_SHORT.search(line)
    if m:
        d = _date(today.year, *m.groups())
        if d and (today - d).days > 180:  # 12월에 '1/5까지' → 내년
            d = _date(today.year + 1, *m.groups())
        return d
    return None


def row_date(cells: list[str], today: dt.date) -> dt.date | None:
    """목록 한 행에서 날짜: 2026-11-18 / 26.11.18 / 11-18 / 오늘이면 '14:32' 만 있는 경우도."""
    line = " ".join(cells)
    for c in cells:  # '16시간 전', '5분 전', '방금', '어제', '2일 전'
        c = c.strip()
        if re.fullmatch(r"(방금|\d+\s*(초|분|시간)\s*전)", c):
            return today  # 하루 안쪽 — 어제 오후일 수도 있지만 '새 공문'으로 보면 됨
        m = re.fullmatch(r"(\d+)\s*일\s*전", c)
        if m:
            return today - dt.timedelta(days=int(m.group(1)))
        if c in ("어제", "어제 "):
            return today - dt.timedelta(days=1)
    m = FULL_DATE.search(line)
    if m:
        return _date(*m.groups())
    m = re.search(r"(?<!\d)(\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})(?!\d)", line)
    if m:
        return _date(2000 + int(m.group(1)), m.group(2), m.group(3))
    for c in cells:
        c = c.strip()
        m = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})\.?", c)
        if m:
            d = _date(today.year, *m.groups())
            if d and d > today:
                d = _date(today.year - 1, *m.groups())
            return d
        if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", c):
            return today
    return None


def _title(line: str) -> str:
    cells = [c.strip() for c in line.split("|") if c.strip()]
    best = max(cells, key=len) if cells else line
    return re.sub(r"\s+", " ", best)[:80]


def _snippet(body: str, words) -> str:
    for ln in body.splitlines():
        ln = ln.strip()
        if 10 <= len(ln) <= 200 and any(w in ln for w in words):
            return ln[:90]
    return ""


def rules_docs(docs: list[dict], today: dt.date, cfg: dict) -> tuple[list[dict], int]:
    """공람 공문: 내 앞으로 온 것이라 기본은 남김. 인사발령·모금처럼 학과 업무와 무관한 것만 빼고,
    제출·신청 단어가 있으면 '제출'."""
    kw = cfg.get("keywords") or DEFAULT_KEYWORDS
    topics = cfg.get("topics") or DEFAULT_TOPICS
    skip = cfg.get("skip_words") or DEFAULT_SKIP
    dept = cfg.get("department", "실용음악학과")
    items, skipped = [], 0
    for d in docs:
        title = re.sub(r"\s+", " ", d["title"])[:80]
        text = f"{d['title']}\n{d['body']}"
        related = dept in text or any(w in text for w in kw)
        all_dept = any(w in text for w in ALL_DEPT_WORDS) or any(w in text for w in topics)
        todo = any(w in text for w in TODO_WORDS)
        unrelated = any(w in d["title"] for w in skip)
        if unrelated and not related:
            skipped += 1
            continue
        if not (related or all_dept or todo or cfg.get("keep_all_by_default", True)):
            skipped += 1
            continue
        due = _due(text, today)
        if todo:
            kind = "제출"
        else:
            kind = "관련"
        items.append({
            "kind": kind, "title": title, "sender": "", "date": d["date"],
            "due": due.isoformat() if due else "",
            "summary": _snippet(d["body"], (dept, *kw) if kind == "관련" else TODO_WORDS),
            "action": f"{due.month}/{due.day}까지 처리" if due else ("내용 확인" if kind == "제출" else ""),
            "urgent": bool(due and (due - today).days <= 3),
        })
    return items, skipped


def with_rules(pages: list[dict], today: dt.date, cfg: dict | None = None) -> dict:
    cfg = cfg or {}
    items, seen, skipped = [], set(), 0
    for p in pages:
        if p.get("counts_only"):
            continue
        if "docs" in p:
            got, sk = rules_docs(p["docs"], today, cfg)
            items += got
            skipped += sk
            continue
        lines = p["rows"] or [ln for ln in p["text"].splitlines() if FULL_DATE.search(ln)]
        for line in lines[:40]:
            title = _title(line)
            if len(title) < 4 or title in seen:
                continue
            years = [int(y) for y in YEAR.findall(title)]
            if years and max(years) < today.year:  # '2019학년도 …' 같은 지난 해 문서
                continue
            dates = [d for d in (_date(*m) for m in FULL_DATE.findall(line)) if d]
            date = min(dates) if dates else None
            due = _due(line, today)
            if due or any(w in line for w in TODO_WORDS):
                kind = "제출"
            else:
                kind = "관련"
            if kind == "관련" and date and (today - date).days > 30:
                continue
            seen.add(title)
            items.append({
                "kind": kind, "title": title, "sender": "", "summary": "",
                "urgent": bool(due and (due - today).days <= 3),
                "date": date.isoformat() if date else "", "due": due.isoformat() if due else "",
                "action": f"{due.month}/{due.day}까지 처리" if due else ("내용 확인" if kind == "제출" else ""),
            })
    n_todo = sum(it["kind"] == "제출" for it in items)
    n_urgent = sum(it["urgent"] for it in items)
    headline = (f"해야 할 일 {n_todo}건" + (f", 그중 마감 임박 {n_urgent}건" if n_urgent else "")) if n_todo else ""
    return {"headline": headline, "skipped": skipped, "items": items}


# ───────── 선택 ─────────
def summarize(pages: list[dict], today: dt.date, cfg: dict, api_key: str | None) -> tuple[dict, str]:
    mode = cfg.get("summarizer", "auto")
    tried = []
    if mode in ("auto", "claude_cli"):
        cli = cfg.get("claude_path") and os.path.expanduser(cfg["claude_path"]) or find_claude_cli()
        if cli:
            try:
                return with_claude_cli(pages, today, cli, cfg), "Claude(구독)"
            except Exception as e:
                log.warning("Claude Code 요약 실패, 다음 방식으로: %s", e)
                tried.append("claude_cli")
        elif mode == "claude_cli":
            log.warning("claude 명령을 찾지 못했습니다. config.yaml 에 claude_path 를 넣어주세요.")
    if mode == "api" and api_key:
        try:
            return with_api(pages, today, api_key, cfg.get("model", "claude-opus-5-5"), cfg), "Claude API"
        except Exception as e:
            log.warning("Claude API 요약 실패: %s", e)
    return with_rules(pages, today, cfg), "규칙"
