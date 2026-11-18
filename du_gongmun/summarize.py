"""그룹웨어 화면 내용 → 할일/공문/일정 목록.

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
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["공문", "일정", "할일"]},
                    "title": {"type": "string"},
                    "sender": {"type": "string"},
                    "date": {"type": "string"},
                    "due": {"type": "string"},
                    "action": {"type": "string"},
                    "urgent": {"type": "boolean"},
                },
                "required": ["kind", "title", "sender", "date", "due", "action", "urgent"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["headline", "items"],
    "additionalProperties": False,
}

SYSTEM = """당신은 동서울대학교 교수의 아침 업무 비서입니다.
그룹웨어 화면에서 긁어 온 텍스트(메뉴, 버튼 등 잡음 포함)를 받아, 교수가 오늘 알아야 할 것만 뽑습니다.

- kind: 받은 공문/결재 문서는 "공문", 날짜가 정해진 회의·행사는 "일정", 제출·회신·결재·확인처럼 교수가 직접 해야 하는 일은 "할일".
- title: 문서/일정 제목을 짧게. sender: 발신 부서나 사람(모르면 빈 문자열).
- date: 접수일이나 일정 날짜, due: 마감·회신 기한. 둘 다 YYYY-MM-DD, 모르면 빈 문자열.
- action: 교수가 해야 할 행동을 한 문장으로(예: "11/25까지 성적 이의신청 결과 입력"). 단순 참고면 "참고".
- urgent: 마감이 3일 이내이거나 이미 지났으면 true.
- 메뉴 이름, 광고, 공지 배너처럼 실제 문서가 아닌 것은 넣지 마세요. 화면에 없는 내용을 지어내지 마세요.
- 오래된 것(30일 이상 지난 공문으로 할 일이 없는 것)은 빼세요.
- headline: 오늘 가장 중요한 것 한 줄 요약."""

WEEKDAYS = "월화수목금토일"


def _prompt_body(pages: list[dict], today: dt.date) -> str:
    body = "\n\n".join(
        f"=== 화면: {p['name']} ({p['url']}) ===\n{p['text'] or '(읽기 실패: ' + p.get('error', '') + ')'}"
        for p in pages
    )
    return f"오늘은 {today.isoformat()} ({WEEKDAYS[today.weekday()]}요일)입니다.\n\n{body}"


def _valid(data: dict) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("요약 결과 형식이 올바르지 않습니다.")
    keys = SCHEMA["properties"]["items"]["items"]["required"]
    items = []
    for it in data["items"]:
        if not isinstance(it, dict) or not it.get("title"):
            continue
        it = {k: it.get(k, False if k == "urgent" else "") for k in keys}
        if it["kind"] not in ("공문", "일정", "할일"):
            it["kind"] = "공문"
        it["urgent"] = bool(it["urgent"])
        items.append(it)
    return {"headline": str(data.get("headline", "")), "items": items}


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


def with_claude_cli(pages: list[dict], today: dt.date, cli: str) -> dict:
    prompt = (
        SYSTEM
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
def with_api(pages: list[dict], today: dt.date, api_key: str, model: str) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    resp = client.beta.messages.create(
        model=model,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": _prompt_body(pages, today)}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude가 요약을 거절했습니다.")
    return _valid(json.loads(next(b.text for b in resp.content if b.type == "text")))


# ───────── 규칙 기반 (완전 무료) ─────────
FULL_DATE = re.compile(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")
DUE_FULL = re.compile(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?\s*(?:\([^)]{1,3}\))?\s*(?:\d{1,2}:\d{2}\s*)?까지")
DUE_SHORT = re.compile(r"(?<![\d.])(\d{1,2})\s*[./월]\s*(\d{1,2})\s*일?\s*(?:\([^)]{1,3}\))?\s*(?:\d{1,2}:\d{2}\s*)?까지")
YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
TODO_WORDS = ("제출", "회신", "요청", "마감", "기한", "까지", "결재", "검토", "입력", "신청", "작성", "조사", "협조")
EVENT_WORDS = ("회의", "행사", "설명회", "워크숍", "세미나", "간담회", "교육", "시험", "특강", "위원회")


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


def _title(line: str) -> str:
    cells = [c.strip() for c in line.split("|") if c.strip()]
    best = max(cells, key=len) if cells else line
    return re.sub(r"\s+", " ", best)[:80]


def with_rules(pages: list[dict], today: dt.date) -> dict:
    items, seen = [], set()
    for p in pages:
        is_calendar = any(w in p["name"] for w in ("일정", "캘린더", "달력"))
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
            if is_calendar or (any(w in line for w in EVENT_WORDS) and date and date >= today):
                kind = "일정"
            elif due or any(w in line for w in TODO_WORDS):
                kind = "할일"
            else:
                kind = "공문"
            if kind == "공문" and date and (today - date).days > 30:
                continue
            if kind == "일정" and date and date < today:
                continue
            seen.add(title)
            items.append({
                "kind": kind, "title": title, "sender": "", "urgent": bool(due and (due - today).days <= 3),
                "date": date.isoformat() if date else "", "due": due.isoformat() if due else "",
                "action": f"{due.month}/{due.day}까지 처리" if due else ("내용 확인" if kind == "할일" else ""),
            })
    n_todo = sum(it["kind"] == "할일" for it in items)
    n_urgent = sum(it["urgent"] for it in items)
    headline = f"할 일 {n_todo}건" + (f", 그중 마감 임박 {n_urgent}건" if n_urgent else "") if items else ""
    return {"headline": headline, "items": items}


# ───────── 선택 ─────────
def summarize(pages: list[dict], today: dt.date, cfg: dict, api_key: str | None) -> tuple[dict, str]:
    mode = cfg.get("summarizer", "auto")
    tried = []
    if mode in ("auto", "claude_cli"):
        cli = cfg.get("claude_path") and os.path.expanduser(cfg["claude_path"]) or find_claude_cli()
        if cli:
            try:
                return with_claude_cli(pages, today, cli), "Claude(구독)"
            except Exception as e:
                log.warning("Claude Code 요약 실패, 다음 방식으로: %s", e)
                tried.append("claude_cli")
        elif mode == "claude_cli":
            log.warning("claude 명령을 찾지 못했습니다. config.yaml 에 claude_path 를 넣어주세요.")
    if mode == "api" and api_key:
        try:
            return with_api(pages, today, api_key, cfg.get("model", "claude-opus-5-5")), "Claude API"
        except Exception as e:
            log.warning("Claude API 요약 실패: %s", e)
    return with_rules(pages, today), "규칙"
