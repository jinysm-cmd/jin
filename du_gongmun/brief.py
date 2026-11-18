#!/usr/bin/env python3
"""동서울대 그룹웨어 공문 아침 브리핑.

매번 새로 로그인 → 설정한 화면(공문 수신함, 일정 등)의 내용을 읽기 →
'처리할 공문 / 일정 / 할 일' 정리 → 맥 알림·아이메시지·메일 등으로 보내기.

사용법:
  python brief.py            # 실제 실행 (카톡 전송)
  python brief.py --dry-run  # 보내지 않고 화면에만 출력
  python brief.py --debug    # 각 화면 스크린샷/HTML을 debug/ 폴더에 저장 (설정 맞출 때)
  python brief.py --show     # 브라우저 창을 띄워서 진행 과정 보기
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import pathlib
import sys
import traceback

import keyring
import yaml

import notify
import summarize

HERE = pathlib.Path(__file__).resolve().parent
SERVICE = "du-gongmun"
STATE_FILE = HERE / "state.json"
DEBUG_DIR = HERE / "debug"
MAX_PAGE_CHARS = 60_000
WEEKDAYS = "월화수목금토일"

log = logging.getLogger("brief")


# ───────────────────────── 설정 ─────────────────────────
def load_config(path: pathlib.Path) -> dict:
    if not path.exists():
        sys.exit(f"설정 파일이 없습니다: {path}\nconfig.example.yaml 을 config.yaml 로 복사해서 채워주세요.")
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for k in ("login_url", "username", "pages"):
        if not cfg.get(k):
            sys.exit(f"config.yaml 에 '{k}' 값이 필요합니다.")
    return cfg


# ───────────────────────── 브라우저 ─────────────────────────
def _frames(page):
    return [f for f in page.frames if not f.is_detached()]


def _settle(page, ms: int = 1500) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        pass
    page.wait_for_timeout(ms)


def login(page, cfg: dict, password: str) -> None:
    sel = cfg.get("selectors") or {}
    pw_sel = sel.get("password", "input[type=password]")
    page.goto(cfg["login_url"], wait_until="domcontentloaded", timeout=45_000)
    _settle(page)

    target = next((f for f in _frames(page) if f.locator(pw_sel).count()), None)
    if target is None:
        raise RuntimeError("로그인 화면에서 비밀번호 입력칸을 찾지 못했습니다 (--debug 로 화면을 확인하세요).")

    if sel.get("username"):
        user = target.locator(sel["username"]).first
    else:  # 비밀번호 칸 앞의 보이는 텍스트 입력칸
        user = target.locator(
            "input[type=text]:visible, input[type=email]:visible, input:not([type]):visible"
        ).first
    user.fill(cfg["username"])
    pw = target.locator(pw_sel).first
    pw.fill(password)
    if sel.get("submit"):
        target.locator(sel["submit"]).first.click()
    else:
        pw.press("Enter")
    _settle(page, 3000)

    still_login = any(f.locator(f"{pw_sel}:visible").count() for f in _frames(page))
    if still_login and page.url.split("?")[0] == cfg["login_url"].split("?")[0]:
        raise RuntimeError("로그인에 실패한 것 같습니다 (아이디/비밀번호 또는 로그인 화면 구조 확인 필요).")
    log.info("로그인 완료: %s", page.url)


def click_text(page, text: str):
    """화면(모든 프레임)에서 text가 보이는 요소를 클릭. 새 창이 뜨면 그 창을 돌려준다."""
    ctx = page.context
    for frame in _frames(page):
        for loc in (frame.get_by_text(text, exact=True), frame.get_by_text(text)):
            for i in range(min(loc.count(), 8)):
                el = loc.nth(i)
                try:
                    if not el.is_visible():
                        continue
                except Exception:
                    continue
                before = len(ctx.pages)
                el.click(timeout=10_000)
                page.wait_for_timeout(2500)
                if len(ctx.pages) > before:  # 팝업/새 탭
                    new = ctx.pages[-1]
                    _settle(new)
                    log.info("'%s' → 새 창 %s", text, new.url)
                    return new
                _settle(page)
                log.info("'%s' 클릭 → %s", text, page.url)
                return page
    raise RuntimeError(f"화면에서 '{text}' 메뉴를 찾지 못했습니다.")


def page_text(page) -> str:
    chunks = []
    for f in _frames(page):
        try:
            t = f.inner_text("body", timeout=5_000).strip()
        except Exception:
            continue
        if t:
            chunks.append(t)
    text = "\n\n".join(chunks)
    return text[:MAX_PAGE_CHARS]


def table_rows(page, limit: int = 30) -> list[str]:
    """Claude 없이 쓸 때: 표의 행을 그대로 뽑는다."""
    rows = []
    for f in _frames(page):
        try:
            for tr in f.locator("tr").all()[:200]:
                cells = [c.strip() for c in tr.locator("td").all_inner_texts() if c.strip()]
                if len(cells) >= 2:
                    rows.append(" | ".join(cells))
        except Exception:
            continue
    return rows[:limit]


def dump_debug(page, name: str) -> None:
    DEBUG_DIR.mkdir(exist_ok=True)
    safe = "".join(c if c.isalnum() else "_" for c in name)
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{safe}.png"), full_page=True)
    except Exception:
        pass
    for i, f in enumerate(_frames(page)):
        try:
            (DEBUG_DIR / f"{safe}_frame{i}.html").write_text(
                f"<!-- {f.url} -->\n" + f.content(), encoding="utf-8")
        except Exception:
            pass


def extract_counts(text: str, labels: list[str]) -> list[str]:
    """'결재대기 0건', '수/발신대기 0 / 0 건' 같은 현황 숫자를 뽑는다."""
    import re
    out = []
    for label in labels:
        m = re.search(re.escape(label) + r"\s*(\d+(?:\s*/\s*\d+)*)\s*건", text)
        if m:
            num = re.sub(r"\s+", "", m.group(1))
            out.append(f"{label} {num}건")
    return out


def wait_counts(page, labels: list[str], max_wait_s: int = 30) -> None:
    """'조회중' 문구가 사라지고 숫자가 두 번 연속 같게 읽힐 때까지(최대 max_wait_s초) 기다린다."""
    import time
    deadline, prev = time.time() + max_wait_s, None
    page.wait_for_timeout(3000)
    while time.time() < deadline:
        text = page_text(page)
        cur = extract_counts(text, labels)
        if "조회중" not in text and len(cur) == len(labels) and cur == prev:
            return
        prev = cur
        page.wait_for_timeout(3000)
    log.warning("현황 숫자가 %d초 안에 다 뜨지 않았습니다. 인터넷이 느릴 수 있어요.", max_wait_s)


def collect(cfg: dict, password: str, *, headless: bool, debug: bool) -> list[dict]:
    from playwright.sync_api import sync_playwright

    results = []
    with sync_playwright() as p:
        launch = {"headless": headless}
        if cfg.get("browser_channel"):  # 예: "chrome" (맥에 설치된 크롬 사용)
            launch["channel"] = cfg["browser_channel"]
        if cfg.get("browser_path"):
            launch["executable_path"] = cfg["browser_path"]
        browser = p.chromium.launch(**launch)
        ctx = browser.new_context(locale="ko-KR", viewport={"width": 1400, "height": 1000})
        page = ctx.new_page()
        page.on("dialog", lambda d: d.accept())  # '비밀번호 변경 안내' 같은 알림창 자동 닫기
        try:
            login(page, cfg, password)
            if debug:
                dump_debug(page, "00_after_login")
            home_url = page.url
            for n, spec in enumerate(cfg["pages"], 1):
                name = spec.get("name", f"page{n}")
                try:
                    cur = page
                    if cur.url != home_url and not spec.get("url"):
                        cur.goto(home_url, wait_until="domcontentloaded")
                        _settle(cur)
                    if spec.get("url"):
                        cur.goto(spec["url"], wait_until="domcontentloaded", timeout=45_000)
                        _settle(cur)
                    for label in spec.get("click") or []:
                        cur = click_text(cur, label)
                    if spec.get("wait_ms"):
                        cur.wait_for_timeout(int(spec["wait_ms"]))
                    if spec.get("counts"):  # 숫자가 늦게 뜨는 화면: 다 뜰 때까지 기다림
                        wait_counts(cur, spec["counts"], int(spec.get("max_wait_s", 30)))
                    if debug:
                        dump_debug(cur, f"{n:02d}_{name}")
                    text = page_text(cur)
                    results.append({"name": name, "url": cur.url, "text": text,
                                    "rows": table_rows(cur),
                                    "counts": extract_counts(text, spec.get("counts") or [])})
                    if cur is not page:
                        cur.close()
                except Exception as e:  # 한 화면이 실패해도 나머지는 계속
                    log.warning("'%s' 읽기 실패: %s", name, e)
                    if debug:  # 새 창이 열려 있었다면 그 화면들도 모두 저장
                        for k, pg in enumerate(ctx.pages):
                            dump_debug(pg, f"{n:02d}_{name}_ERROR_{k}")
                    for pg in ctx.pages[1:]:
                        pg.close()
                    results.append({"name": name, "url": "", "text": "", "rows": [],
                                    "error": str(e)})
        except Exception:
            if debug:
                dump_debug(page, "99_error")
            raise
        finally:
            browser.close()
    return results


# ───────────────────────── 상태(새 공문 표시) ─────────────────────────
def item_key(it: dict) -> str:
    return hashlib.sha1(f"{it['kind']}|{it['title']}|{it['date']}".encode()).hexdigest()[:16]


def mark_new(items: list[dict]) -> None:
    try:
        seen = set(json.loads(STATE_FILE.read_text())["seen"])
    except Exception:
        seen = set()
    for it in items:
        it["new"] = item_key(it) not in seen
    seen |= {item_key(it) for it in items}
    STATE_FILE.write_text(json.dumps({"seen": sorted(seen)[-2000:]}))


# ───────────────────────── 메시지 ─────────────────────────
def _md(s: str) -> str:
    try:
        d = dt.date.fromisoformat(s)
        return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"
    except ValueError:
        return s


def format_brief(summary: dict, today: dt.date, errors: list[str], counts: list[str] = ()) -> str:
    items = summary["items"]
    sort_key = lambda it: (not it["urgent"], it["due"] or it["date"] or "9999")  # noqa: E731
    lines = [f"📋 {today.month}/{today.day}({WEEKDAYS[today.weekday()]}) 공문 브리핑"]
    if counts:
        lines.append("📊 " + " · ".join(counts))
    if summary.get("headline"):
        lines.append(summary["headline"])
    sections = [("할일", "✅ 해야 할 일"), ("공문", "📌 공문"), ("일정", "📅 일정")]
    for kind, title in sections:
        group = sorted((it for it in items if it["kind"] == kind), key=sort_key)
        if not group:
            continue
        lines += ["", f"{title} ({len(group)})"]
        for it in group:
            mark = "🔴" if it["urgent"] else ("🆕" if it.get("new") else "·")
            when = f" ~{_md(it['due'])}" if it["due"] else (f" {_md(it['date'])}" if it["date"] and kind == "일정" else "")
            lines.append(f"{mark} {it['title']}{when}")
            if it["action"] and it["action"] != "참고" and kind != "일정":
                lines.append(f"   → {it['action']}")
    if not items:
        lines += ["", "새로 처리할 공문이 없습니다. 👍"]
    if errors:
        lines += ["", "⚠️ 못 읽은 화면: " + ", ".join(errors)]
    return "\n".join(lines)


def save_brief(text: str, today: dt.date, cfg: dict) -> pathlib.Path:
    out_dir = pathlib.Path(cfg.get("output_dir", "~/Documents/공문브리핑")).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{today.isoformat()}.txt"
    path.write_text(text, encoding="utf-8")
    return path


# ───────────────────────── main ─────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "config.yaml"))
    ap.add_argument("--dry-run", action="store_true", help="보내지 않고 출력만")
    ap.add_argument("--debug", action="store_true", help="화면 스크린샷/HTML 저장")
    ap.add_argument("--show", action="store_true", help="브라우저 창 띄우기")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(pathlib.Path(a.config))
    today = dt.date.today()

    def notify_error(msg: str) -> None:
        log.error(msg)
        if not a.dry_run:
            notify.deliver(f"⚠️ 공문 브리핑 실패\n{msg[:300]}", {**cfg, "open_file": False}, None)

    password = keyring.get_password(SERVICE, "portal-password")
    if not password:
        sys.exit("포털 비밀번호가 키체인에 없습니다. `python setup_secrets.py` 를 먼저 실행하세요.")

    try:
        pages = collect(cfg, password, headless=not a.show and cfg.get("headless", True), debug=a.debug)
    except Exception as e:
        notify_error(f"포털 접속/로그인 오류: {e}")
        traceback.print_exc()
        return 1
    errors = [p["name"] for p in pages if p.get("error")]

    summary, how = summarize.summarize(pages, today, cfg,
                                       keyring.get_password(SERVICE, "anthropic-api-key"))
    log.info("요약 방식: %s", how)
    mark_new(summary["items"])
    counts = [c for p in pages for c in p.get("counts", [])]
    text = format_brief(summary, today, errors, counts)
    path = save_brief(text, today, cfg)
    print(text)
    log.info("브리핑 저장: %s", path)

    if not a.dry_run and notify.deliver(text, cfg, path):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
