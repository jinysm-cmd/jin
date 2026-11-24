"""국내 주식 세력 흔적 전략 백테스트.

python krx_backtest.py                  # 셋업별 성과 (전체 기간 / IS / OOS)
python krx_backtest.py --optimize       # IS 기간에서 파라미터를 고르고 OOS 로 검증
python krx_backtest.py --limit 300      # 시가총액 상위 300 종목만 (빠른 확인용)
"""
import argparse
import itertools
import logging
from dataclasses import replace

import pandas as pd

from krx import engine, strategy
from krx.data import fetch_daily, list_stocks, load_all

IS_END = "2021-12-31"
OOS_START = "2022-01-01"


def fmt(s: dict) -> str:
    if not s:
        return "거래 없음"
    return (f"CAGR {s['cagr']:+6.1%}  총 {s['total']:+7.1%}  MDD {s['mdd']:6.1%}  샤프 {s['sharpe']:4.2f}  "
            f"거래 {s['trades']:4d}  승률 {s.get('winrate', 0):5.1%}  평균 {s.get('avg_ret', 0):+5.2%}  "
            f"PF {s.get('pf', 0):4.2f}  보유 {s.get('avg_bars', 0):4.1f}일")


def build(data, p):
    sigs = {c: strategy.compute(df, p) for c, df in data.items()}
    return sigs, engine.prepare(data, sigs)


def periods(prep, book, cfg):
    out = {}
    for name, (a, b) in {"전체": ("2016-01-01", "2100-01-01"), "IS ~2021": ("2016-01-01", IS_END),
                         "OOS 2022~": (OOS_START, "2100-01-01")}.items():
        out[name] = engine.run(prep, book, replace(cfg, start=a, end=b))
    return out


def benchmark():
    rows = {}
    for sym in ("KOSPI", "KOSDAQ"):
        df = fetch_daily(sym)["close"]
        for name, (a, b) in {"전체": ("2016-01-01", "2100"), "IS ~2021": ("2016-01-01", IS_END),
                             "OOS 2022~": (OOS_START, "2100")}.items():
            s = df[(df.index >= a) & (df.index <= b)]
            rows.setdefault(sym, {})[name] = engine.stats(s, pd.DataFrame())
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--optimize", action="store_true")
    ap.add_argument("--max-pos", type=int, default=10)
    ap.add_argument("--trades-csv", default="krx_trades.csv")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    uni = list_stocks().sort_values("market_value", ascending=False)
    codes = list(uni["code"][: args.limit] if args.limit else uni["code"])
    data = load_all(codes)
    print(f"종목 {len(data)}개 로드\n")

    print("== 벤치마크 (지수 보유) ==")
    for sym, r in benchmark().items():
        for name, s in r.items():
            print(f"{sym:7s} {name:10s} CAGR {s['cagr']:+6.1%}  총 {s['total']:+7.1%}  MDD {s['mdd']:6.1%}  샤프 {s['sharpe']:4.2f}")
    print()

    p = strategy.Params()
    sigs, prep = build(data, p)
    base = engine.EngineCfg(max_pos=args.max_pos)
    print("== 셋업별 (기본 파라미터) ==")
    best_tr = None
    for setups in (("A",), ("B",), ("C",), ("A", "B")):
        book = engine.signal_book(sigs, setups)
        res = periods(prep, book, replace(base, setups=setups))
        for name, r in res.items():
            print(f"{'+'.join(setups):4s} {name:10s} {fmt(r.stats)}")
        if setups == ("A", "B"):
            best_tr = res["전체"].trades
        print()
    if best_tr is not None and len(best_tr):
        best_tr.to_csv(args.trades_csv, index=False)
        print("청산 사유별 (A+B 전체):")
        print(best_tr.groupby("why")["ret"].agg(["count", "mean"]).to_string(), "\n")

    if not args.optimize:
        return
    print("== 최적화: IS(~2021) 에서 고르고 OOS(2022~) 로 검증 ==")
    grid = {
        "brk_tv_mult": [2.0, 3.0, 5.0],
        "pb_vol_ratio": [0.4, 0.6],
        "box_max_width": [0.3, 0.5],
    }
    eng_grid = {"trail": [0.08, 0.15], "max_hold": [10, 30]}
    rows = []
    for vals in itertools.product(*grid.values()):
        pp = replace(p, **dict(zip(grid, vals)))
        sg, pr = build(data, pp)
        for setups in (("A",), ("B",), ("A", "B")):
            book = engine.signal_book(sg, setups)
            for ev in itertools.product(*eng_grid.values()):
                cfg = replace(base, setups=setups, **dict(zip(eng_grid, ev)))
                is_ = engine.run(pr, book, replace(cfg, end=IS_END)).stats
                oos = engine.run(pr, book, replace(cfg, start=OOS_START)).stats
                rows.append({**dict(zip(grid, vals)), **dict(zip(eng_grid, ev)), "setups": "+".join(setups),
                             **{f"is_{k}": v for k, v in is_.items()}, **{f"oos_{k}": v for k, v in oos.items()}})
    df = pd.DataFrame(rows)
    df = df[df["is_trades"] >= 100]
    df.to_csv("krx_optimize.csv", index=False)
    top = df.sort_values("is_sharpe", ascending=False).head(10)
    cols = ["setups", *grid, *eng_grid, "is_cagr", "is_mdd", "is_sharpe", "is_trades", "oos_cagr", "oos_mdd", "oos_sharpe", "oos_trades"]
    pd.set_option("display.width", 250)
    print("IS 샤프 상위 10개 설정과 그 OOS 성과:")
    print(top[cols].to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print(f"\n전체 {len(df)}개 설정 중 OOS CAGR > 0 비율: {(df['oos_cagr'] > 0).mean():.0%}")
    print(f"IS 상위 10개의 OOS 평균 CAGR: {top['oos_cagr'].mean():+.1%}, 전체 평균: {df['oos_cagr'].mean():+.1%}")


if __name__ == "__main__":
    main()
