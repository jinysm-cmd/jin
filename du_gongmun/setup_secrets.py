"""비밀번호를 macOS 키체인에 저장합니다 (이 맥에만 저장, 파일이나 다른 곳에 남지 않음).
   python setup_secrets.py          포털 비밀번호 (+ 메일 쓰면 지메일 앱 비밀번호)
   python setup_secrets.py --api    (선택, 유료) Claude API 키"""
import getpass
import sys

import keyring
import yaml

SERVICE = "du-gongmun"

if "--api" in sys.argv:
    key = getpass.getpass("Claude API 키 (sk-ant-...): ").strip()
    if key:
        keyring.set_password(SERVICE, "anthropic-api-key", key)
        print("✔ 저장. config.yaml 에 summarizer: api 로 바꾸면 사용합니다.")
    sys.exit()

pw = getpass.getpass("동서울대 포털 비밀번호 (입력해도 화면에 안 보입니다): ")
if pw:
    keyring.set_password(SERVICE, "portal-password", pw)
    print("✔ 포털 비밀번호 저장")

try:
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8")) or {}
except FileNotFoundError:
    cfg = {}
if "email" in (cfg.get("delivery") or []):
    app_pw = getpass.getpass("지메일 앱 비밀번호 16자리 (README 참고, 없으면 Enter): ").strip()
    if app_pw:
        keyring.set_password(SERVICE, "gmail-app-password", app_pw)
        print("✔ 지메일 앱 비밀번호 저장")
