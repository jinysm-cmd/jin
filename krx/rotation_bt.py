"""조용한 강세주 로테이션 백테스트 (현물 롱 전용).

점수 = 52주 고점 근접도 + 6개월 모멘텀 (좋은 쪽)
       - 최근 거래대금 폭발 - 최근 5일 급등 - 변동성 (세력이 넘기는 흔적, 나쁜 쪽)
R 거래일마다 어제 종가 점수로 상위 N 종목을 고르고 오늘 시가에 교체한다.
빠지는 종목은 시가 매도, 새 종목은 시가 매수, 남는 종목은 동일 비중으로 맞춤(차액에만 비용).
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .engine import FEE, SLIP, sell_tax, stats


@dataclass
class RotCfg:
    top_n: int = 20
    rebal: int = 20
    min_tv20: float = 3e9
    min_price: float = 1000.0
    w_high: float = 1.0
    w_mom: float = 1.0
    w_spike: float = 1.0
    w_ret5: float = 1.0
    w_vol: float = 1.0
    regime: bool = False          # 지수가 200일선 아래면 현금
    buffer: int = 0               # 보유 종목은 상위 N+buffer 안이면 유지 (회전율 감소)
    start: str = "2016-01-01"
    end: str = "2100-01-01"


def wide(data: dict):
    o = pd.DataFrame({c: d["open"] for c, d in data.items()}).sort_index()
    h = pd.DataFrame({c: d["high"] for c, d in data.items()}).reindex(o.index)
    c_ = pd.DataFrame({c: d["close"] for c, d in data.items()}).reindex(o.index)
    v = pd.DataFrame({c: d["volume"] for c, d in data.items()}).reindex(o.index)
    return o, h, c_, v


def score_panel(o, h, c, v, cfg: RotCfg) -> pd.DataFrame:
    tv = c * v
    tv20 = tv.rolling(20, min_periods=20).mean()
    nbar = c.notna().cumsum()
    elig = (tv20 >= cfg.min_tv20) & (c >= cfg.min_price) & (nbar >= 250)
    feats = {
        "high": (c / h.rolling(250, min_periods=200).max() - 1, cfg.w_high),
        "mom": (c.shift(20) / c.shift(120) - 1, cfg.w_mom),
        "spike": (-(tv.rolling(5).max() / tv.shift(5).rolling(60).mean()), cfg.w_spike),
        "ret5": (-(c / c.shift(5) - 1), cfg.w_ret5),
        "vol": (-c.pct_change(fill_method=None).rolling(60).std(), cfg.w_vol),
    }
    sc = None
    for f, w in feats.values():
        if w == 0:
            continue
        r = f.where(elig).rank(axis=1, pct=True) * w
        sc = r if sc is None else sc + r
    return sc.where(elig & sc.notna())


def run(o, c, score: pd.DataFrame, cfg: RotCfg, index_close: pd.Series | None = None):
    dates = o.index[(o.index >= cfg.start) & (o.index <= cfg.end)]
    pos: dict[str, float] = {}  # code -> 평가금액 (직전 종가 기준)
    cash = 1.0
    eq, trades_n, turnover = [], 0, 0.0
    risk_on = pd.Series(True, index=o.index)
    if cfg.regime and index_close is not None:
        ic = index_close.reindex(o.index).ffill()
        risk_on = (ic > ic.rolling(200).mean()).shift(1).fillna(False)
    cols = {cd: i for i, cd in enumerate(o.columns)}
    O, C, S = o.to_numpy(), c.to_numpy(), score.to_numpy()
    prev_i = None
    for k, d in enumerate(dates):
        i = o.index.get_loc(d)
        if prev_i is not None and k % cfg.rebal == 0:
            row = S[prev_i]
            valid = ~np.isnan(row)
            target = []
            if risk_on.iloc[i] and valid.any():
                order = np.argsort(-np.where(valid, row, -np.inf))
                ranked = [o.columns[j] for j in order[: valid.sum()]]
                keep_zone = set(ranked[: cfg.top_n + cfg.buffer])
                target = [cd for cd in pos if cd in keep_zone]
                for cd in ranked:
                    if len(target) >= cfg.top_n:
                        break
                    if cd not in target and not np.isnan(O[i, cols[cd]]):
                        target.append(cd)
            tset = set(target)
            # 시가로 평가 이동 (갭 반영)
            for cd in list(pos):
                j = cols[cd]
                if not np.isnan(O[i, j]) and not np.isnan(C[prev_i, j]):
                    pos[cd] *= O[i, j] / C[prev_i, j]
            # 매도
            for cd in list(pos):
                if cd not in tset and not np.isnan(O[i, cols[cd]]):
                    amt = pos.pop(cd)
                    cash += amt * (1 - SLIP - FEE - sell_tax(d))
                    turnover += amt
                    trades_n += 1
            total = cash + sum(pos.values())
            tgt = total / cfg.top_n if target else 0
            for cd in target:
                cur = pos.get(cd, 0.0)
                delta = tgt - cur
                if delta > 0:
                    delta = min(delta, cash)
                    cash -= delta
                    pos[cd] = cur + delta * (1 - SLIP - FEE)
                    if cur == 0:
                        trades_n += 1
                elif delta < 0:
                    pos[cd] = cur + delta
                    cash += -delta * (1 - SLIP - FEE - sell_tax(d))
                turnover += abs(delta)
            # 시가 → 종가
            for cd in pos:
                j = cols[cd]
                if not np.isnan(C[i, j]) and not np.isnan(O[i, j]):
                    pos[cd] *= C[i, j] / O[i, j]
        elif prev_i is not None:
            for cd in pos:
                j = cols[cd]
                if not np.isnan(C[i, j]) and not np.isnan(C[prev_i, j]):
                    pos[cd] *= C[i, j] / C[prev_i, j]
        eq.append(cash + sum(pos.values()))
        prev_i = i
    equity = pd.Series(eq, index=dates)
    st = stats(equity, pd.DataFrame())
    yrs = max((dates[-1] - dates[0]).days / 365.25, 1e-9)
    st.update({"trades": trades_n, "turnover_yr": turnover / yrs})
    return equity, st
