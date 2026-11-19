"""카카오톡 '나에게 보내기' (나와의 채팅) 전송.

토큰은 macOS 키체인에 저장합니다. 리프레시 토큰은 사용할 때마다 자동 갱신되므로
매일 실행되는 한 다시 로그인할 필요가 없습니다.
"""
from __future__ import annotations

import json
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import keyring
import requests

SERVICE = "du-gongmun"
TOKEN_KEY = "kakao-token"
AUTH_URL = "https://kauth.kakao.com/oauth/authorize"
TOKEN_URL = "https://kauth.kakao.com/oauth/token"
MEMO_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
TEXT_LIMIT = 200  # 카카오 기본 텍스트 템플릿 글자 수 제한


def _load() -> dict:
    raw = keyring.get_password(SERVICE, TOKEN_KEY)
    if not raw:
        raise RuntimeError("카카오 연결이 안 되어 있습니다. `python kakao.py setup` 을 먼저 실행하세요.")
    return json.loads(raw)


def _save(tok: dict) -> None:
    keyring.set_password(SERVICE, TOKEN_KEY, json.dumps(tok))


def setup(rest_api_key: str, client_secret: str = "") -> None:
    """브라우저로 카카오 로그인 → 토큰을 받아 키체인에 저장."""
    got: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = parse_qs(urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("카카오 연결 완료. 이 창은 닫으셔도 됩니다.".encode())

        def log_message(self, *a):
            pass

    params = {"client_id": rest_api_key, "redirect_uri": REDIRECT_URI,
              "response_type": "code", "scope": "talk_message"}
    url = f"{AUTH_URL}?{urlencode(params)}"
    print("브라우저에서 카카오 로그인 후 '동의'를 눌러주세요.\n열리지 않으면 이 주소를 직접 여세요:\n" + url)
    webbrowser.open(url)
    server = HTTPServer(("localhost", REDIRECT_PORT), Handler)
    while "code" not in got and "error" not in got:
        server.handle_request()
    if "error" in got:
        raise RuntimeError(f"카카오 로그인 실패: {got}")

    data = {"grant_type": "authorization_code", "client_id": rest_api_key,
            "redirect_uri": REDIRECT_URI, "code": got["code"]}
    if client_secret:
        data["client_secret"] = client_secret
    r = requests.post(TOKEN_URL, data=data, timeout=20)
    if r.status_code != 200:
        hint = ""
        if r.status_code == 401 or "client" in r.text:
            hint = ("\n→ Client Secret 이 켜져 있는데 입력하지 않았거나 틀린 경우가 대부분입니다."
                    "\n  카카오 개발자 > 앱 > 플랫폼 키 > REST API 키 화면(또는 카카오 로그인 > 보안)의"
                    "\n  '클라이언트 시크릿' 코드를 복사해서 setup 을 다시 실행하세요.")
        raise SystemExit(f"카카오 토큰 발급 실패 ({r.status_code}): {r.text[:300]}{hint}")
    tok = r.json()
    tok.update(rest_api_key=rest_api_key, client_secret=client_secret,
               expires_at=time.time() + tok.get("expires_in", 0))
    _save(tok)
    print("카카오 토큰 저장 완료.")


def _access_token() -> str:
    tok = _load()
    if time.time() < tok.get("expires_at", 0) - 300:
        return tok["access_token"]
    data = {"grant_type": "refresh_token", "client_id": tok["rest_api_key"],
            "refresh_token": tok["refresh_token"]}
    if tok.get("client_secret"):
        data["client_secret"] = tok["client_secret"]
    r = requests.post(TOKEN_URL, data=data, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"카카오 토큰 갱신 실패 ({r.status_code}): {r.text[:200]} — `python kakao.py setup` 다시 실행")
    new = r.json()
    tok["access_token"] = new["access_token"]
    tok["expires_at"] = time.time() + new.get("expires_in", 0)
    if new.get("refresh_token"):  # 만료 1개월 전부터 새 리프레시 토큰이 같이 옴
        tok["refresh_token"] = new["refresh_token"]
    _save(tok)
    return tok["access_token"]


def chunk(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """줄 단위로 끊어 limit 글자 이하 조각으로 나눈다."""
    parts, cur = [], ""
    for line in text.splitlines():
        while len(line) > limit:  # 한 줄이 너무 긴 경우
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:limit])
            line = line[limit:]
        cand = f"{cur}\n{line}" if cur else line
        if len(cand) > limit:
            parts.append(cur)
            cur = line
        else:
            cur = cand
    if cur.strip():
        parts.append(cur)
    return [p for p in parts if p.strip()]


def send(text: str, link_url: str = "", max_messages: int = 8) -> int:
    """text를 나와의 채팅으로 보낸다. 보낸 메시지 수를 돌려준다."""
    pieces = chunk(text)
    if len(pieces) > max_messages:
        rest = len(pieces) - (max_messages - 1)
        pieces = pieces[: max_messages - 1] + [f"…나머지 {rest}개 메시지 분량은 맥의 브리핑 파일을 확인하세요."]
    headers = {"Authorization": f"Bearer {_access_token()}"}
    for i, p in enumerate(pieces):
        obj = {"object_type": "text", "text": p,
               "link": {"web_url": link_url, "mobile_web_url": link_url}}
        if link_url:
            obj["button_title"] = "포털 열기"
        r = requests.post(MEMO_URL, headers=headers,
                          data={"template_object": json.dumps(obj, ensure_ascii=False)}, timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"카카오 전송 실패 ({r.status_code}): {r.text[:200]}")
        if i < len(pieces) - 1:
            time.sleep(0.5)
    return len(pieces)


if __name__ == "__main__":
    import argparse
    import getpass

    ap = argparse.ArgumentParser(description="카카오톡 나에게 보내기 설정/테스트")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="카카오 로그인해서 토큰 저장")
    t = sub.add_parser("test", help="테스트 메시지 보내기")
    t.add_argument("text", nargs="?", default="✅ 공문 브리핑 카카오 연결 테스트입니다.")
    a = ap.parse_args()
    if a.cmd == "setup":
        key = input("카카오 REST API 키: ").strip()
        secret = getpass.getpass("Client Secret (사용 안 하면 그냥 Enter): ").strip()
        setup(key, secret)
    else:
        print(f"{send(a.text)}개 메시지 전송")
