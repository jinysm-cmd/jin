import os

import yaml


def load_config(path: str = "config.yaml") -> dict:
    if not os.path.exists(path):
        path = "config.example.yaml"
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    # 바이낸스 앱 ROI(증거금 대비) 기준 익절 → 가격 변동 % 로 변환
    roi = cfg.get("take_profit_roi") or 0
    if roi > 0:
        cfg.setdefault("strategy", {})["tp_price_pct"] = roi / float(cfg["leverage"])
    return cfg
