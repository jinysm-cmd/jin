"""외국인/기관 수급 특성별 향후 수익률 조사 (시가총액 상위 종목).

python krx_flow_research.py            # 향후 20일
python krx_flow_research.py --hz 5
"""
import argparse
import glob
import os

import numpy as np
import pandas as pd

from krx_research import IS_END, features


def flow_features(px: pd.DataFrame, fl: pd.DataFrame) -> pd.DataFrame:
    fl = fl.reindex(px.index)
    vol = fl["volume"]
    smart = fl["foreign"] + fl["organ"]
    f = pd.DataFrame(index=px.index)
    for k in (5, 20, 60):
        vs = vol.rolling(k, min_periods=k).sum()
        f[f"frg{k}"] = fl["foreign"].rolling(k).sum() / vs
        f[f"org{k}"] = fl["organ"].rolling(k).sum() / vs
        f[f"smart{k}"] = smart.rolling(k).sum() / vs
        f[f"ind{k}"] = fl["indiv"].rolling(k).sum() / vs
    f["fhold_chg20"] = fl["foreign_ratio"] - fl["foreign_ratio"].shift(20)
    f["fhold_chg60"] = fl["foreign_ratio"] - fl["foreign_ratio"].shift(60)
    # 동시 매수 일수: 최근 20일 중 외국인과 기관이 둘 다 순매수한 날
    f["both_days20"] = ((fl["foreign"] > 0) & (fl["organ"] > 0)).astype(float).rolling(20).sum()
    # 조용한 매집: 수급은 강한데 주가는 덜 오름 (수급 순위 - 수익률 순위는 횡단면에서 계산)
    return f


def load_panel(min_tv20: float):
    parts = []
    for path in glob.glob("data/krx/flow/*.csv"):
        code = os.path.basename(path)[:6]
        pp = f"data/krx/daily/{code}.csv"
        if not os.path.exists(pp):
            continue
        px = pd.read_csv(pp, index_col=0, parse_dates=True)
        fl = pd.read_csv(path, index_col=0, parse_dates=True)
        if len(px) < 300 or len(fl) < 300:
            continue
        f = features(px).join(flow_features(px, fl))
        f = f[(f["tv20"] >= min_tv20) & (f["close"] >= 1000) & (f["nbar"] >= 250)]
        f["code"] = code
        parts.append(f)
    p = pd.concat(parts)
    p.index.name = "date"
    p = p.reset_index()
    for hz in (5, 20, 60):
        p[f"ex{hz}"] = p[f"fwd{hz}"] - p.groupby("date")[f"fwd{hz}"].transform("mean")
    r = lambda col: p.groupby("date")[col].rank(pct=True)  # noqa: E731
    p["quiet_accum"] = r("smart20") - r("ret20")
    return p


def qtable(p, feat, hz):
    q = p.groupby("date")[feat].transform(
        lambda s: pd.qcut(s.rank(method="first"), 5, labels=False) if s.notna().sum() >= 25 else np.nan)
    p = p.assign(q=q).dropna(subset=["q", f"ex{hz}"])
    p["period"] = np.where(p["date"] <= IS_END, "IS", "OOS")
    t = p.groupby(["period", "q"])[f"ex{hz}"].mean().unstack("q") * 100
    t.columns = [f"Q{int(c) + 1}" for c in t.columns]
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hz", type=int, default=20)
    ap.add_argument("--min-tv20", type=float, default=3e9)
    a = ap.parse_args()
    p = load_panel(a.min_tv20)
    print(f"종목-일 {len(p):,}, 종목 {p.code.nunique()}개. 향후 {a.hz}일 초과수익(%), Q1=낮음 Q5=높음\n")
    feats = [c for c in p.columns if c.startswith(("frg", "org", "smart", "ind", "fhold", "both"))] + ["quiet_accum"]
    rows = []
    for ft in feats:
        t = qtable(p, ft, a.hz)
        spread = t["Q5"] - t["Q1"]
        rows.append({"feat": ft, "IS_Q5-Q1": spread.get("IS"), "OOS_Q5-Q1": spread.get("OOS"),
                     "IS_Q5": t.loc["IS", "Q5"], "OOS_Q5": t.loc["OOS", "Q5"],
                     "IS_Q1": t.loc["IS", "Q1"], "OOS_Q1": t.loc["OOS", "Q1"]})
        print(f"[{ft}]\n{t.round(2).to_string()}\n")
    out = pd.DataFrame(rows)
    out.to_csv("krx_flow_research.csv", index=False)
    print("== 요약: 상위 20% - 하위 20% (두 기간 같은 부호면 일관됨) ==")
    print(out.round(2).to_string(index=False))


if __name__ == "__main__":
    main()
