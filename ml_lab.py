"""AI(머신러닝) 예측 전략 vs 현재 전략 비교 실험.

  python3 ml_lab.py 2>&1 | tee ml_result.txt

- 4시간봉, 거래대금 상위 30개, 3년 데이터
- 앞 50% 로 첫 학습 → 뒤 50% 를 4구간으로 나눠, 각 구간은 그 이전 데이터만으로 다시 학습해서 예측
- 같은 뒤 50% 기간에서 현재 전략(하이킨아시+눌림목)과 성과 비교
- 'AI 필터': 현재 전략 신호 중 AI 가 익절 확률을 높게 본 것만 진입
- AUC: 모델의 예측력. 0.5 = 동전 던지기, 0.55 이상이면 의미 있는 예측력
"""
import argparse
import logging

import numpy as np
import pandas as pd

from bot import ml
from bot.backtest_engine import Costs, portfolio, simulate_signals, simulate_symbol, summarize
from bot.config import load_config
from bot.data import load_klines, top_symbols
from bot.exchange import BinanceFutures
from bot.strategy import StrategyParams, generate_signals

THRESHOLDS = [0.40, 0.45, 0.50, 0.55]
FILTER_THRESHOLDS = [0.30, 0.35, 0.40]  # 현재 전략 신호 중 AI 확률이 이 이상인 것만 진입
WARMUP = 210  # 신규 상장 직후처럼 지표가 덜 계산된 봉은 제외


def stats_row(name, tdf, curve):
    st = summarize(tdf, curve)
    if st["trades"] == 0:
        return {"전략": name, "거래": 0}
    return {"전략": name, "거래": st["trades"], "하루": st["trades_per_day"], "승률%": st["win_rate"],
            "avg_R": st["avg_R"], "PF": st["profit_factor"], "수익%": st["total_return_%"],
            "MDD%": st["max_drawdown_%"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config_4h.yaml")
    ap.add_argument("--days", type=int, default=1095)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    from sklearn.metrics import roc_auc_score

    cfg = load_config(args.config)
    costs = Costs(**cfg.get("costs", {}))
    client = BinanceFutures()
    u = cfg["universe"]
    symbols = top_symbols(client, u["top_n"], u["min_quote_volume"], u.get("exclude"))
    if "BTCUSDT" not in symbols:
        symbols.append("BTCUSDT")
    data = {}
    for s in symbols:
        df = load_klines(client, s, cfg["interval"], args.days)
        if len(df) >= 1000:
            data[s] = df

    print(f"\n데이터셋 구성 중... ({len(data)}개 코인)")
    ds = ml.build_dataset(data)
    feat_cols = [c for c in ds.columns if c not in ("time", "symbol", "atr", "y_long", "y_short")]
    test_start = ds["time"].min() + (ds["time"].max() - ds["time"].min()) * 0.5
    print(f"특징 {len(feat_cols)}개, 학습 샘플 {len(ds):,}개, 검증 시작 {test_start:%Y-%m-%d}")
    print("모델 학습 중 (구간별 4회 x 롱/숏)...")
    preds = ml.walk_forward_predict(ds, feat_cols, test_start)
    ds = pd.concat([ds, preds], axis=1)

    te = ds[ds["time"] >= test_start]
    print("\n===== 모델 예측력 (한 번도 학습에 안 쓴 기간) =====")
    for side in ("long", "short"):
        m = te.dropna(subset=[f"y_{side}", f"p_{side}"])
        auc = roc_auc_score(m[f"y_{side}"], m[f"p_{side}"])
        print(f"  {side:>5}: AUC {auc:.3f} | 실제 익절 비율 {m[f'y_{side}'].mean() * 100:.1f}% "
              f"| 예측 확률 평균 {m[f'p_{side}'].mean() * 100:.1f}%")

    p = StrategyParams.from_dict(cfg.get("strategy", {}))
    rows = []
    for th in THRESHOLDS:
        trades = []
        for s, df in data.items():
            z = ds[ds.symbol == s].set_index("time").reindex(df.index)
            f = df.copy()
            pl, ps = z["p_long"].fillna(0), z["p_short"].fillna(0)
            sig = np.where((pl >= th) & (pl >= ps), 1, np.where(ps >= th, -1, 0))
            sig[:WARMUP] = 0
            sig[z["atr"].isna().to_numpy()] = 0
            f["signal"] = sig
            f["setup"] = np.where(sig != 0, "ML", "")
            sl = np.maximum(ml.SL_ATR * z["atr"], f["close"] * p.min_sl_pct)
            f["sl_dist"] = sl.where(sig != 0)
            f["tp_dist"] = (sl * ml.TP_ATR / ml.SL_ATR).where(sig != 0)
            trades += simulate_signals(s, f, p, costs, cfg.get("cooldown_bars", 3))
        tdf, curve = portfolio(trades, cfg["risk_per_trade"], cfg["leverage"], cfg["max_positions"])
        rows.append(stats_row(f"AI 확률≥{th:.2f}", tdf, curve))
        print(f"  완료: AI 임계값 {th}")

    for th in FILTER_THRESHOLDS:
        trades = []
        for s, df in data.items():
            z = ds[ds.symbol == s].set_index("time").reindex(df.index)
            f = generate_signals(df, p)
            pl, ps = z["p_long"].fillna(0), z["p_short"].fillna(0)
            keep = ((f["signal"] == 1) & (pl >= th)) | ((f["signal"] == -1) & (ps >= th))
            f.loc[~keep, "signal"] = 0
            trades += [t for t in simulate_signals(s, f, p, costs, cfg.get("cooldown_bars", 3))
                       if t.entry_time >= test_start]
        tdf, curve = portfolio(trades, cfg["risk_per_trade"], cfg["leverage"], cfg["max_positions"])
        rows.append(stats_row(f"현재 전략 + AI필터≥{th:.2f}", tdf, curve))
        print(f"  완료: 현재 전략 + AI 필터 {th}")

    base = [t for s, df in data.items()
            for t in simulate_symbol(s, df, p, costs, cooldown_bars=cfg.get("cooldown_bars", 3))
            if t.entry_time >= test_start]
    tdf, curve = portfolio(base, cfg["risk_per_trade"], cfg["leverage"], cfg["max_positions"])
    rows.append(stats_row("현재 전략(하이킨+눌림목)", tdf, curve))

    pd.set_option("display.width", 200)
    print(f"\n===== 같은 검증 기간({test_start:%Y-%m-%d} ~) 성과 비교, 수수료 포함 =====")
    print(pd.DataFrame(rows).to_string(index=False))
    print("\n주의: 임계값 4개 중 가장 좋은 것을 고르면 그 자체로 약간의 '결과 보고 고르기'가 됩니다.")


if __name__ == "__main__":
    main()
