"""수급 + 가격 로테이션.

python krx_flow_rotation.py              # 현재 시총 상위 400 (생존 편향 큼)
python krx_flow_rotation.py --pit        # 2017년 이후 한 번이라도 거래대금 100억 이상이었던 종목 (편향 작음)

IS(2017-06~2021) 에서 각 수급 특성의 방향을 정하고, 같은 설정을 OOS(2022~) 에 그대로 적용한다.
"""
import argparse
import itertools
from dataclasses import replace

import pandas as pd

from krx import rotation_bt as rb
from krx.data import fetch_daily, list_stocks, load_all
from krx.engine import stats
from krx.flow import load_flows

P = {"IS": ("2017-06-01", "2021-12-31"), "OOS": ("2022-01-01", "2100-01-01")}


def flow_panels(flows: dict, index) -> dict:
    def wide(col):
        return pd.DataFrame({c: f[col] for c, f in flows.items()}).reindex(index)
    vol, frg, org, ind, fr = (wide(k) for k in ("volume", "foreign", "organ", "indiv", "foreign_ratio"))
    v60 = vol.rolling(60, min_periods=60).sum()
    return {
        "ind60": ind.rolling(60).sum() / v60,
        "neg_org60": -(org.rolling(60).sum() / v60),
        "fhold60": fr - fr.shift(60),
        "both20": ((frg > 0) & (org > 0)).astype(float).where(vol.notna()).rolling(20).sum(),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pit", action="store_true")
    args = ap.parse_args()
    uni = list_stocks().sort_values("market_value", ascending=False)
    if args.pit:
        codes = open("data/krx/pit_universe.txt").read().split()
    else:
        codes = list(uni["code"][:400])
    flows = load_flows(codes, max_age_h=1e9)
    data = {k: v for k, v in load_all(codes).items() if k in flows and len(v)}
    o, h, c, v = rb.wide(data)
    fp = flow_panels(flows, c.index)
    k = fetch_daily("KOSPI")["close"]
    print(f"종목 {c.shape[1]}개\n")
    for n, (a, b) in P.items():
        s = stats(k[a:b], pd.DataFrame())
        print(f"KOSPI {n:3s}  CAGR {s['cagr']:+6.1%}  MDD {s['mdd']:6.1%}  샤프 {s['sharpe']:.2f}")
    el = ((c * v).rolling(20).mean() >= 1e10) & (c >= 1000) & (c.notna().cumsum() >= 250)
    ew = (1 + c.pct_change(fill_method=None).where(el.shift(1)).mean(axis=1).fillna(0)).cumprod()
    for n, (a, b) in P.items():
        s = stats(ew[a:b], pd.DataFrame())
        print(f"같은 종목 동일비중 {n:3s}  CAGR {s['cagr']:+6.1%}  MDD {s['mdd']:6.1%}  샤프 {s['sharpe']:.2f}")
    print()

    price_only = dict(w_mom=0, w_spike=0, w_ret5=0)  # 52주 고점 + 저변동성
    combos = {
        "가격만 (고점+저변동)": ({}, price_only),
        "가격 + 개인60": ({"ind60": 1.0}, price_only),
        "가격 + 기관매도60": ({"neg_org60": 1.0}, price_only),
        "가격 + 외국인지분↑": ({"fhold60": 1.0}, price_only),
        "가격 + 수급4종": ({"ind60": 1.0, "neg_org60": 1.0, "fhold60": 1.0, "both20": 1.0}, price_only),
        "수급4종만": ({"ind60": 1.0, "neg_org60": 1.0, "fhold60": 1.0, "both20": 1.0},
                    dict(w_high=0, w_vol=0, **price_only)),
    }
    rows = []
    for (name, (ex, w)), top_n, rebal in itertools.product(combos.items(), (10, 20), (20, 60)):
        cfg = rb.RotCfg(min_tv20=1e10, top_n=top_n, rebal=rebal, buffer=top_n // 2, **w)
        sc = rb.score_panel(o, h, c, v, cfg, {kk: (fp[kk], ww) for kk, ww in ex.items()})
        r = {n: rb.run(o, c, sc, replace(cfg, start=a, end=b))[1] for n, (a, b) in P.items()}
        rows.append({"전략": name, "N": top_n, "교체": rebal,
                     **{f"{n}_{m}": r[n][m] for n in P for m in ("cagr", "mdd", "sharpe")}})
        print(f"{name:16s} N{top_n:2d} {rebal:2d}일 | IS CAGR {r['IS']['cagr']:+6.1%} MDD {r['IS']['mdd']:6.1%} 샤프 {r['IS']['sharpe']:.2f}"
              f" | OOS CAGR {r['OOS']['cagr']:+6.1%} MDD {r['OOS']['mdd']:6.1%} 샤프 {r['OOS']['sharpe']:.2f}", flush=True)
    pd.DataFrame(rows).to_csv("krx_flow_rotation_pit.csv" if args.pit else "krx_flow_rotation.csv", index=False)


if __name__ == "__main__":
    main()
