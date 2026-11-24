"""거래량/거래대금/가격 특성별 향후 수익률 조사.

각 날짜에서 유동성 있는 종목만 놓고 특성을 5분위로 나눈 뒤,
다음 날 시가에 사서 h 거래일 뒤 종가에 판 수익률에서 같은 날 전체 평균을 뺀 '초과수익'을 본다.
IS(~2021) 와 OOS(2022~) 에서 같은 방향으로 나와야 쓸 만한 특성이다.
"""
import argparse
import glob
import os

import numpy as np
import pandas as pd

IS_END = pd.Timestamp("2021-12-31")


def features(df: pd.DataFrame) -> pd.DataFrame:
    o, h, l, c, v = (df[k] for k in ("open", "high", "low", "close", "volume"))
    tv = c * v
    tv20p = tv.shift(1).rolling(20).mean()
    rng = (h - l).replace(0, np.nan)
    ret = c.pct_change()
    f = pd.DataFrame(index=df.index)
    f["tv20"] = tv.rolling(20).mean()
    f["tv_spike"] = tv / tv20p                         # 오늘 거래대금 / 평소
    f["vol_dry"] = v.rolling(5).mean() / v.rolling(60).mean()   # 최근 거래량 마름
    f["tv_trend"] = tv.rolling(20).mean() / tv.rolling(120).mean()  # 거래대금 추세
    f["ret1"] = ret
    f["ret5"] = c / c.shift(5) - 1
    f["ret20"] = c / c.shift(20) - 1
    f["ret120"] = c.shift(20) / c.shift(120) - 1       # 중기 모멘텀 (최근 1달 제외)
    f["from_high"] = c / h.rolling(250).max() - 1      # 52주 고점 대비
    f["clv"] = ((c - l) / rng).fillna(0.5)
    f["volat"] = ret.rolling(20).std()
    f["upday_vol"] = (v.where(ret > 0, 0).rolling(20).sum() / v.where(ret < 0, 0).rolling(20).sum().replace(0, np.nan))
    f["nbar"] = np.arange(len(df))
    f["close"] = c
    for hz in (5, 20, 60):
        f[f"fwd{hz}"] = c.shift(-hz) / o.shift(-1) - 1
    f["next_gap"] = o.shift(-1) / c - 1
    return f


def load_panel(min_tv20: float) -> pd.DataFrame:
    parts = []
    for path in glob.glob("data/krx/daily/*.csv"):
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        if len(df) < 300:
            continue
        f = features(df)
        f = f[(f["tv20"] >= min_tv20) & (f["close"] >= 1000) & (f["nbar"] >= 250)]
        f["code"] = os.path.basename(path)[:6]
        parts.append(f)
    p = pd.concat(parts)
    p.index.name = "date"
    p = p.reset_index()
    for hz in (5, 20, 60):
        p[f"ex{hz}"] = p[f"fwd{hz}"] - p.groupby("date")[f"fwd{hz}"].transform("mean")
    return p


def quintile_table(p: pd.DataFrame, feat: str, hz: int) -> pd.DataFrame:
    q = p.groupby("date")[feat].transform(lambda s: pd.qcut(s.rank(method="first"), 5, labels=False) if s.notna().sum() >= 20 else np.nan)
    p = p.assign(q=q).dropna(subset=["q", f"ex{hz}"])
    p["period"] = np.where(p["date"] <= IS_END, "IS", "OOS")
    t = p.groupby(["period", "q"])[f"ex{hz}"].mean().unstack("period") * 100
    t.index = [f"Q{int(i) + 1}" for i in t.index]
    return t


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-tv20", type=float, default=3e9)
    ap.add_argument("--hz", type=int, default=20)
    args = ap.parse_args()
    p = load_panel(args.min_tv20)
    print(f"종목-일 {len(p):,}개, 종목 {p.code.nunique()}개")
    print(f"향후 {args.hz}일 초과수익(%) — Q1=특성값 가장 낮음, Q5=가장 높음\n")
    feats = ["tv_spike", "vol_dry", "tv_trend", "upday_vol", "ret1", "ret5", "ret20", "ret120", "from_high", "clv", "volat"]
    rows = []
    for ft in feats:
        t = quintile_table(p, ft, args.hz)
        rows.append({"feat": ft, **{f"{per}_{q}": t.loc[q, per] for q in t.index for per in t.columns}})
        print(f"[{ft}]")
        print(t.round(2).T.to_string(), "\n")
    pd.DataFrame(rows).to_csv("krx_research.csv", index=False)


if __name__ == "__main__":
    main()
