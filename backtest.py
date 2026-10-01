"""백테스트 실행기.

예)
  python backtest.py --days 120                       # config 의 심볼(auto=거래대금 상위)로 120일
  python backtest.py --symbols BTCUSDT,ETHUSDT,SOLUSDT --days 180
  python backtest.py --days 180 --optimize            # 앞 70% 로 파라미터 탐색 → 뒤 30% 로 검증
  python backtest.py --interval 1h --days 365 --top 40 --set squeeze_mode=fade --set trap_enabled=false
  python backtest.py --vision-dir data/vision --symbols BTCUSDT   # data.binance.vision CSV 사용
"""
import argparse
import itertools
import logging
import time

import pandas as pd
import yaml

from bot.backtest_engine import Costs, breakdown, portfolio, simulate_symbol, summarize
from bot.config import load_config
from bot.data import load_klines, load_vision_dir, top_symbols
from bot.exchange import BinanceFutures
from bot.strategy import StrategyParams

GRID = {
    "dev_k": [1.8, 2.2, 2.6],
    "absorb_vol_z": [1.5, 2.0],
    "trap_imb": [0.1, 0.2],
    "squeeze_rank_max": [0.15, 0.3],
    "max_hold_bars": [24, 48],
}


def run(datasets, fundings, params, cfg, costs):
    trades = []
    for sym, df in datasets.items():
        trades += simulate_symbol(sym, df, params, costs, fundings.get(sym), cfg.get("cooldown_bars", 3))
    tdf, curve = portfolio(trades, cfg["risk_per_trade"], cfg["leverage"], cfg["max_positions"])
    return tdf, curve


def split(datasets, fundings, frac):
    a, b = {}, {}
    for s, df in datasets.items():
        cut = df.index[int(len(df) * frac)]
        a[s], b[s] = df[df.index < cut], df[df.index >= cut]
    return a, b


def print_report(title, tdf, curve):
    print(f"\n===== {title} =====")
    for k, v in summarize(tdf, curve).items():
        print(f"  {k:>16}: {v}")
    if not tdf.empty:
        print("\n  [셋업별]")
        print(breakdown(tdf, "setup").to_string())
        print("\n  [심볼별]")
        print(breakdown(tdf, "symbol").to_string())


def print_split(tdf, frac=0.7):
    """앞 70% / 뒤 30% 기간을 나눠서 성과가 둘 다 유지되는지 확인."""
    if tdf.empty:
        return
    t0, t1 = tdf.entry_time.min(), tdf.entry_time.max()
    cut = t0 + (t1 - t0) * frac
    print("\n  [기간 분할 검증] 둘 다 avg_R 플러스여야 우연이 아닐 가능성이 높음")
    for name, part in (("앞 70%", tdf[tdf.entry_time < cut]), ("뒤 30%", tdf[tdf.entry_time >= cut])):
        if len(part):
            w = part[part.pnl > 0].pnl.sum()
            l = -part[part.pnl < 0].pnl.sum()
            pf = round(w / l, 2) if l > 0 else float("inf")
            print(f"    {name}: 거래 {len(part)}회, 승률 {(part.net_ret > 0).mean() * 100:.1f}%, "
                  f"avg_R {part.r_multiple.mean():+.3f}, PF {pf}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--symbols", default=None, help="콤마 구분. 생략 시 config")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--interval", default=None)
    ap.add_argument("--optimize", action="store_true", help="IS 70%% 에서 그리드 탐색 후 OOS 30%% 검증")
    ap.add_argument("--no-funding", action="store_true")
    ap.add_argument("--vision-dir", default=None)
    ap.add_argument("--out", default="backtest_trades.csv")
    ap.add_argument("--max-positions", type=int, default=None, help="동시 최대 포지션 수 (config 대신)")
    ap.add_argument("--top", type=int, default=None, help="거래대금 상위 N개 (config 의 top_n 대신)")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="전략 파라미터 덮어쓰기. 여러 번 사용 가능")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    cfg = load_config(args.config)
    interval = args.interval or cfg["interval"]
    costs = Costs(**cfg.get("costs", {}))
    for kv in args.set:
        k, v = kv.split("=", 1)
        if k not in StrategyParams.__dataclass_fields__:
            raise SystemExit(f"알 수 없는 파라미터: {k}")
        cfg.setdefault("strategy", {})[k] = yaml.safe_load(v)
    if args.top:
        cfg["universe"]["top_n"] = args.top
    if args.max_positions:
        cfg["max_positions"] = args.max_positions
    params = StrategyParams.from_dict(cfg.get("strategy"))
    if args.set:
        print("파라미터 덮어쓰기:", ", ".join(args.set))
    client = BinanceFutures()

    if args.symbols:
        symbols = args.symbols.split(",")
    elif cfg["symbols"] == "auto":
        u = cfg["universe"]
        symbols = top_symbols(client, u["top_n"], u["min_quote_volume"], u.get("exclude"))
    else:
        symbols = cfg["symbols"]
    print("심볼:", ", ".join(symbols))

    datasets, fundings = {}, {}
    for s in symbols:
        df = load_vision_dir(args.vision_dir, s, interval) if args.vision_dir else load_klines(client, s, interval, args.days)
        if len(df) < 1000:
            print(f"  {s}: 데이터 부족({len(df)}봉) 제외")
            continue
        datasets[s] = df
        if not args.no_funding and not args.vision_dir:
            st, en = int(df.index[0].value // 1e6), int(time.time() * 1000)
            fundings[s] = client.funding_history(s, st, en)

    if not args.optimize:
        tdf, curve = run(datasets, fundings, params, cfg, costs)
        print_report(f"백테스트 {interval} / {len(datasets)}개 심볼", tdf, curve)
        print_split(tdf)
        if not tdf.empty:
            tdf.to_csv(args.out, index=False)
            print(f"\n거래 내역 저장: {args.out}")
        return

    is_data, oos_data = split(datasets, fundings, 0.7)
    keys = list(GRID)
    results = []
    combos = list(itertools.product(*GRID.values()))
    print(f"\n그리드 {len(combos)}개 조합 탐색 (IS 구간)...")
    for vals in combos:
        p = StrategyParams.from_dict({**cfg.get("strategy", {}), **dict(zip(keys, vals))})
        tdf, curve = run(is_data, fundings, p, cfg, costs)
        s = summarize(tdf, curve)
        if s["trades"] < 30:
            continue
        # 수익률/낙폭 비율로 평가 (과최적화 방지 위해 거래수 최소 조건)
        score = s["total_return_%"] / max(1.0, abs(s["max_drawdown_%"]))
        results.append((score, dict(zip(keys, vals)), s))
    if not results:
        print("조건을 만족하는 조합이 없습니다.")
        return
    results.sort(key=lambda x: x[0], reverse=True)
    print("\nIS 상위 5개:")
    for score, combo, s in results[:5]:
        print(f"  score={score:.2f} {combo} -> {s}")
    best = results[0][1]
    p = StrategyParams.from_dict({**cfg.get("strategy", {}), **best})
    tdf, curve = run(oos_data, fundings, p, cfg, costs)
    print_report(f"OOS 검증 (처음 보는 30% 구간) 파라미터={best}", tdf, curve)
    print("\nOOS 결과가 IS 보다 크게 나쁘면 과최적화입니다. OOS 에서도 PF>1.2 정도는 나와야 실전 고려 가치가 있습니다.")


if __name__ == "__main__":
    main()
