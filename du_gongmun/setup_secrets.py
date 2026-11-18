"""포털 비밀번호와 (선택) Claude API 키를 macOS 키체인에 저장합니다.
입력한 값은 이 맥의 키체인에만 저장되고 파일이나 다른 곳에는 남지 않습니다."""
import getpass

import keyring

SERVICE = "du-gongmun"

pw = getpass.getpass("동서울대 포털 비밀번호 (입력해도 화면에 안 보입니다): ")
if pw:
    keyring.set_password(SERVICE, "portal-password", pw)
    print("✔ 포털 비밀번호 저장")

key = getpass.getpass("Claude API 키 (sk-ant-..., 없으면 Enter — 요약 없이 목록만 보냅니다): ").strip()
if key:
    keyring.set_password(SERVICE, "anthropic-api-key", key)
    print("✔ Claude API 키 저장")
