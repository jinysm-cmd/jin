"""자동매매 봇 실행.

  python run_bot.py                 # config.yaml 의 mode 로 실행 (기본 paper)
  python run_bot.py --mode testnet
  python run_bot.py --config config_4h.yaml --mode paper --engine heikin
"""
import argparse
import logging
import os

from bot.config import load_config
from bot.trader import Trader


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--mode", choices=["paper", "testnet", "live"], default=None)
    ap.add_argument("--engine", default=None, help="진입 전략 덮어쓰기 (예: heikin 또는 heikin,pullback)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if args.mode:
        cfg["mode"] = args.mode
    if args.engine:
        cfg.setdefault("strategy", {})["engine"] = args.engine
    if cfg["mode"] == "live" and os.environ.get("I_UNDERSTAND_THE_RISK") != "yes":
        raise SystemExit("live 모드는 환경변수 I_UNDERSTAND_THE_RISK=yes 를 설정해야 실행됩니다.")

    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(f"logs/bot_{cfg['mode']}.log", encoding="utf-8")],
    )
    Trader(cfg).run()


if __name__ == "__main__":
    main()
