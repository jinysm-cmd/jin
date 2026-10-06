"""다중 시간프레임 하이킨아시 전략 (1H 추세 → 15M 눌림 → 5M 돌파) + 부분익절/추적청산.

사용자가 제공한 전략 문서를 그대로 옮긴 것. 5분봉 데이터 하나로 1H/15M 을 만들어 쓰며,
상위 시간봉 값은 '그 봉이 마감된 뒤'에만 하위 봉에서 보이도록 맞춰 미래 정보가 섞이지 않게 한다.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import classic
from . import indicators as ind
from .backtest_engine import Costs, Trade
from .data import resample


@dataclass
class MTFParams:
    adx_skip: float = 18.0        # 1H ADX 이 값 미만이면 거래 안 함
    adx_full: float = 25.0        # 18~25 구간은 절반 크기
    setup_valid_bars: int = 3     # 15M 눌림 신호가 유효한 15분봉 개수
    rsi_lo: float = 50.0
    rsi_hi: float = 65.0
    wick_atr: float = 0.15        # '짧은 아래꼬리' 기준: HA 아래꼬리 <= ATR x 이 값
    min_conditions: int = 3       # 5M 보조조건 4개 중 최소 충족 개수 (+ 직전 고점 돌파 필수)
    swing_bars: int = 6           # 손절 기준 스윙 저점 탐색 봉 수 (5분봉)
    sl_atr_min: float = 1.0
    sl_atr_max: float = 1.5
    tp1_r: float = 1.0
    tp1_frac: float = 0.3
    tp2_r: float = 2.0
    tp2_frac: float = 0.4
    cooldown_bars: int = 6


def _ha(df):
    ho, hh, hl, hc = classic.heikin_ashi(df)
    return ho, hh, hl, hc


def _htf_frame(df5: pd.DataFrame, rule: str) -> pd.DataFrame:
    """상위 시간봉 지표를 계산해 '마감 시각' 기준으로 인덱싱."""
    h = resample(df5, rule)
    ho, _, hl, hc = _ha(h)
    c = h["close"]
    out = pd.DataFrame(index=h.index)
    out["ha_green"] = hc > ho
    out["ha_red"] = hc < ho
    out["ha_low"] = hl
    out["ema20"], out["ema50"], out["ema200"] = classic.ema(c, 20), classic.ema(c, 50), classic.ema(c, 200)
    out["close"], out["low"], out["high"] = c, h["low"], h["high"]
    out["adx"] = ind.adx(h, 14)
    out.index = out.index + pd.Timedelta(rule)  # 마감 시각
    return out


def _asof(htf: pd.DataFrame, close_times: pd.DatetimeIndex) -> pd.DataFrame:
    """각 5분봉 마감 시점에 이미 마감된 가장 최근 상위봉 값."""
    return htf.reindex(htf.index.union(close_times)).ffill().reindex(close_times)


def signals(df5: pd.DataFrame, p: MTFParams) -> pd.DataFrame:
    f = df5.copy()
    close_t = f.index + pd.Timedelta("5min")

    h1 = _htf_frame(df5, "1h")
    h1["up"] = h1.ha_green & (h1.ema20 > h1.ema50) & (h1.ema50 > h1.ema200) & (h1.close > h1.ema200)
    h1["dn"] = h1.ha_red & (h1.ema20 < h1.ema50) & (h1.ema50 < h1.ema200) & (h1.close < h1.ema200)

    m15 = _htf_frame(df5, "15min")
    prev = m15.shift(1)
    touch_l = m15["low"].rolling(4).min() <= m15.ema20
    touch_h = m15["high"].rolling(4).max() >= m15.ema20
    m15["setup_l"] = (m15.ema20 > m15.ema50) & touch_l & (m15.close >= m15.ema50) & \
        prev.ha_red & m15.ha_green & (m15.ha_low > prev.ha_low)
    m15["setup_s"] = (m15.ema20 < m15.ema50) & touch_h & (m15.close <= m15.ema50) & \
        prev.ha_green & m15.ha_red
    k = p.setup_valid_bars
    m15["setup_l"] = m15["setup_l"].astype(float).rolling(k, min_periods=1).max() > 0
    m15["setup_s"] = m15["setup_s"].astype(float).rolling(k, min_periods=1).max() > 0

    H = _asof(h1[["up", "dn", "adx"]], close_t)
    M = _asof(m15[["setup_l", "setup_s"]], close_t)

    ho, hh, hl, hc = _ha(df5)
    a = ind.atr(df5, 14)
    e20 = classic.ema(f["close"], 20)
    r = classic.rsi(f["close"], 14)
    green, red = hc > ho, hc < ho
    cond_l = (green.astype(int) + ((ho - hl) <= p.wick_atr * a).astype(int) +
              (f["close"] > e20).astype(int) + r.between(p.rsi_lo, p.rsi_hi).astype(int))
    cond_s = (red.astype(int) + ((hh - ho) <= p.wick_atr * a).astype(int) +
              (f["close"] < e20).astype(int) + r.between(100 - p.rsi_hi, 100 - p.rsi_lo).astype(int))
    brk_l = f["close"] > f["high"].shift(1)
    brk_s = f["close"] < f["low"].shift(1)

    adx = H["adx"].to_numpy()
    ok_adx = adx >= p.adx_skip
    long_ = H["up"].fillna(False).to_numpy(bool) & M["setup_l"].fillna(False).to_numpy(bool) & \
        (cond_l >= p.min_conditions).to_numpy() & brk_l.to_numpy() & ok_adx
    short = H["dn"].fillna(False).to_numpy(bool) & M["setup_s"].fillna(False).to_numpy(bool) & \
        (cond_s >= p.min_conditions).to_numpy() & brk_s.to_numpy() & ok_adx

    swing_l = f["close"] - f["low"].rolling(p.swing_bars).min()
    swing_h = f["high"].rolling(p.swing_bars).max() - f["close"]
    sl_l = swing_l.clip(lower=p.sl_atr_min * a, upper=p.sl_atr_max * a)
    sl_s = swing_h.clip(lower=p.sl_atr_min * a, upper=p.sl_atr_max * a)

    f["signal"] = np.where(long_, 1, np.where(short, -1, 0))
    f["sl_dist"] = np.where(long_, sl_l, np.where(short, sl_s, np.nan))
    f["size"] = np.where(adx >= p.adx_full, 1.0, 0.5)
    f["ha_red"], f["ha_green"], f["ema20"] = red, green, e20
    f.iloc[:2500, f.columns.get_loc("signal")] = 0  # EMA200(1H) 워밍업
    f.loc[~np.isfinite(f["sl_dist"]) | (f["sl_dist"] <= 0), "signal"] = 0
    return f


def simulate(symbol: str, df5: pd.DataFrame, p: MTFParams, costs: Costs) -> list[Trade]:
    f = signals(df5, p)
    o, h, l, c = (f[k].to_numpy() for k in ("open", "high", "low", "close"))
    sig, sld, size = f["signal"].to_numpy(), f["sl_dist"].to_numpy(), f["size"].to_numpy()
    red, green, e20 = f["ha_red"].to_numpy(), f["ha_green"].to_numpy(), f["ema20"].to_numpy()
    idx = f.index
    n = len(f)
    fee = costs.fee + costs.slippage
    trades = []
    i = 0
    while i < n - 1:
        if sig[i] == 0:
            i += 1
            continue
        d, e_i = int(sig[i]), i + 1
        entry, R = o[e_i], sld[i]
        sl = entry - d * R
        tp1, tp2 = entry + d * p.tp1_r * R, entry + d * p.tp2_r * R
        remaining, parts, stage, j = 1.0, [], 0, e_i   # parts: (비율, 청산가)
        while j < n and remaining > 1e-9:
            hit_sl = l[j] <= sl if d == 1 else h[j] >= sl
            if hit_sl:  # 같은 봉에서 손절과 익절이 겹치면 손절 우선 (보수적)
                px = min(o[j], sl) if d == 1 else max(o[j], sl)
                parts.append((remaining, px))
                remaining = 0
                break
            if stage == 0 and ((h[j] >= tp1) if d == 1 else (l[j] <= tp1)):
                parts.append((p.tp1_frac, tp1))
                remaining -= p.tp1_frac
                stage = 1
            if stage == 1 and ((h[j] >= tp2) if d == 1 else (l[j] <= tp2)):
                parts.append((p.tp2_frac, tp2))
                remaining -= p.tp2_frac
                stage = 2
            if stage == 2:  # 남은 30%: HA 추적청산
                trail = (red[j] and (c[j] < e20[j] or red[j - 1])) if d == 1 else \
                    (green[j] and (c[j] > e20[j] or green[j - 1]))
                if trail:
                    parts.append((remaining, c[j]))
                    remaining = 0
                    break
            j += 1
        if remaining > 1e-9:
            parts.append((remaining, c[n - 1]))
            j = n - 1
        gross = sum(w * d * (px - entry) / entry for w, px in parts)
        net = gross - fee * (1 + sum(w for w, _ in parts))   # 진입 1회 + 부분 청산들
        exit_px = sum(w * px for w, px in parts)
        k = size[i]
        trades.append(Trade(
            symbol=symbol, setup="MTF_HA", side=d, entry_time=idx[e_i],
            exit_time=idx[j] + pd.Timedelta("5min"), entry=entry, exit=exit_px, sl_dist=R,
            reason=f"TP{stage}" if stage else "SL", bars=j - e_i + 1,
            gross_ret=gross * k, net_ret=net * k, r_multiple=net * k / (R / entry),
        ))
        i = j + p.cooldown_bars
    return trades
