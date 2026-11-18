#!/bin/bash
# 동서울대 공문 브리핑 설치 (맥).  사용: ./install_mac.sh [시:분]   예) ./install_mac.sh 07:30
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
TIME="${1:-07:30}"
HOUR=$((10#${TIME%%:*})); MIN=$((10#${TIME##*:}))
LABEL="kr.ac.du.gongmun-brief"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "▶ 1/4 파이썬 환경 준비"
command -v python3 >/dev/null || { echo "python3 가 없습니다. https://www.python.org 에서 설치하세요."; exit 1; }
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m playwright install chromium

echo "▶ 2/4 설정 파일"
if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  echo "  config.yaml 을 만들었습니다. 아이디와 포털 주소를 채운 뒤 이 스크립트를 다시 실행하세요."
  open -e config.yaml || true
  exit 0
fi

echo "▶ 3/4 비밀번호 저장 (키체인)"
if ! .venv/bin/python -c "import keyring,sys; sys.exit(0 if keyring.get_password('du-gongmun','portal-password') else 1)"; then
  .venv/bin/python setup_secrets.py
fi
if grep -E '^delivery:.*kakao' config.yaml >/dev/null \
   && ! .venv/bin/python -c "import keyring,sys; sys.exit(0 if keyring.get_password('du-gongmun','kakao-token') else 1)"; then
  .venv/bin/python kakao.py setup
fi
if command -v claude >/dev/null || [ -x "$HOME/.local/bin/claude" ]; then
  echo "  Claude Code 발견 → 구독으로 요약합니다 (추가 요금 없음)."
else
  echo "  Claude Code 없음 → 규칙 방식으로 정리합니다 (무료)."
fi

echo "▶ 4/4 매일 평일 ${HOUR}시 ${MIN}분 자동 실행 등록"
mkdir -p logs "$HOME/Library/LaunchAgents"
{
cat <<XML
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$DIR/.venv/bin/python</string><string>$DIR/brief.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StandardOutPath</key><string>$DIR/logs/brief.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/brief.log</string>
  <key>StartCalendarInterval</key><array>
XML
for d in 1 2 3 4 5; do
  echo "    <dict><key>Weekday</key><integer>$d</integer><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>$MIN</integer></dict>"
done
cat <<XML
  </array>
</dict></plist>
XML
} > "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo
echo "✅ 설치 완료. 지금 한번 시험해 보려면:"
echo "   cd \"$DIR\" && .venv/bin/python brief.py --dry-run --show"
echo "   (알림·메시지까지 보내려면 --dry-run 을 빼세요)"
