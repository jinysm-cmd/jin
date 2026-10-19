"""코인 순환매(롱숏 모멘텀) 봇 실행.

  python3 rotation_bot.py                     # 모의(paper): 실제 시세로 가상 계좌 운용, 주문 없음
  I_UNDERSTAND_THE_RISK=yes python3 rotation_bot.py --mode live

설정은 config_rotation.yaml. 기존 4시간봉 봇과 같은 계정에서 같이 돌려도 되며,
서로 상대가 잡은 코인은 건드리지 않는다.
"""
import argparse
import logging
import os

import yaml

from bot.rotation import Rotator


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config_rotation.yaml")
    ap.add_argument("--mode", choices=["paper", "live"], default="paper")
    ap.add_argument("--now", action="store_true", help="지금 바로 한 번 리밸런스 (다음 예정일 무시)")
    args = ap.parse_args()
    with open(args.config, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    if args.mode == "live" and os.environ.get("I_UNDERSTAND_THE_RISK") != "yes":
        raise SystemExit("live 모드는 환경변수 I_UNDERSTAND_THE_RISK=yes 를 설정해야 실행됩니다.")
    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(f"logs/rotation_{args.mode}.log", encoding="utf-8")],
    )
    r = Rotator(cfg, args.mode)
    if args.now:
        r.rebalance()
    r.run()


if __name__ == "__main__":
    main()
