"""브리핑 보내기 — 전부 무료.

config.yaml 의 delivery 목록에서 고릅니다:
  mac       맥 알림센터 알림 + 브리핑 파일 자동으로 열기
  imessage  아이폰 '메시지'로 나에게 보내기 (맥 메시지 앱에 Apple ID 로그인 필요)
  email     지메일로 나에게 보내기 (구글 '앱 비밀번호' 필요)
  kakao     카카오톡 나에게 보내기 (카카오 개발자 앱 필요)
"""
from __future__ import annotations

import logging
import pathlib
import smtplib
import subprocess
from email.mime.text import MIMEText

import keyring

SERVICE = "du-gongmun"
log = logging.getLogger("brief")

_IMESSAGE_SCRIPT = """
on run argv
  set msg to item 1 of argv
  set handle to item 2 of argv
  tell application "Messages"
    set svc to 1st account whose service type = iMessage
    send msg to participant handle of svc
  end tell
end run
"""


def mac(text: str, file: pathlib.Path | None, open_file: bool = True) -> None:
    lines = text.splitlines()
    title, body = lines[0], " / ".join(l.strip() for l in lines[1:4] if l.strip())
    subprocess.run(["osascript", "-e", "on run argv", "-e",
                    "display notification (item 2 of argv) with title (item 1 of argv) sound name \"Glass\"",
                    "-e", "end run", title, body or "새 항목 없음"], check=True, timeout=30)
    if open_file and file:
        subprocess.run(["open", "-a", "TextEdit", str(file)], check=False, timeout=30)


def imessage(text: str, to: str) -> None:
    if not to:
        raise RuntimeError("config.yaml 의 imessage_to 에 내 전화번호(+8210...)나 Apple ID 이메일을 넣어주세요.")
    r = subprocess.run(["osascript", "-e", _IMESSAGE_SCRIPT, text, to],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"아이메시지 전송 실패: {r.stderr.strip()[:200]}")


def email(text: str, address: str) -> None:
    if not address:
        raise RuntimeError("config.yaml 의 email_to 에 지메일 주소를 넣어주세요.")
    pw = keyring.get_password(SERVICE, "gmail-app-password")
    if not pw:
        raise RuntimeError("지메일 앱 비밀번호가 없습니다. `python setup_secrets.py` 를 실행하세요.")
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = text.splitlines()[0]
    msg["From"] = msg["To"] = address
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(address, pw.replace(" ", ""))
        s.send_message(msg)


def deliver(text: str, cfg: dict, file: pathlib.Path | None) -> list[str]:
    """설정된 곳으로 모두 보낸다. 실패한 방식 이름 목록을 돌려준다."""
    failed = []
    for way in cfg.get("delivery") or ["mac"]:
        try:
            if way == "mac":
                mac(text, file, cfg.get("open_file", True))
            elif way == "imessage":
                imessage(text, str(cfg.get("imessage_to", "")))
            elif way == "email":
                email(text, cfg.get("email_to", ""))
            elif way == "kakao":
                import kakao
                kakao.send(text, cfg.get("link_url", ""), int(cfg.get("max_kakao_messages", 8)))
            else:
                raise RuntimeError(f"알 수 없는 delivery: {way}")
            log.info("%s 전송 완료", way)
        except Exception as e:
            log.error("%s 전송 실패: %s", way, e)
            failed.append(way)
    return failed
