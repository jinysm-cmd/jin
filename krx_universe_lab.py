"""유동성 기준(거래대금)과 점수 요소 조합별 로테이션 비교. IS 는 데이터가 충분해지는 2017-06 부터."""
from dataclasses import replace
import pandas as pd
from krx import rotation_bt as rb
from krx.data import list_stocks, load_all
data = {k: v for k, v in load_all(list(list_stocks()["code"])).items() if len(v) > 0}
o, h, c, v = rb.wide(data)
P = {"IS": ("2017-06-01", "2021-12-31"), "OOS": ("2022-01-01", "2100")}
for tv in (3e9, 1e10, 3e10):
    for name, w in {"all": {}, "high+vol": dict(w_mom=0, w_spike=0, w_ret5=0)}.items():
        for rebal in (20, 60):
            cfg = rb.RotCfg(min_tv20=tv, rebal=rebal, top_n=20, buffer=10, **w)
            sc = rb.score_panel(o, h, c, v, cfg)
            out = []
            for k, (a, b) in P.items():
                s = rb.run(o, c, sc, replace(cfg, start=a, end=b))[1]
                out.append(f"{k} CAGR {s['cagr']:+6.1%} MDD {s['mdd']:6.1%} 샤프 {s['sharpe']:4.2f}")
            print(f"tv20>={tv/1e8:4.0f}억 {name:8s} rebal {rebal:2d} | " + " | ".join(out), flush=True)
    # 유동성 동일비중
    tv20 = (c * v).rolling(20).mean()
    elig = (tv20 >= tv) & (c >= 1000) & (c.notna().cumsum() >= 250)
    ew = (1 + c.pct_change(fill_method=None).where(elig.shift(1)).mean(axis=1).fillna(0)).cumprod()
    from krx.engine import stats
    print(f"tv20>={tv/1e8:4.0f}억 동일비중       | " + " | ".join(
        f"{k} CAGR {stats(ew[a:b], pd.DataFrame())['cagr']:+6.1%}" for k, (a, b) in P.items()), flush=True)
