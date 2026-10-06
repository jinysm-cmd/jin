"""대표 기술적 지표 전략 5종. 교과서적 기본값을 그대로 쓴다(과최적화 방지).

strategy.generate_signals 에서 StrategyParams.engine 으로 선택:
  pullback  : 눌림목 - 정배열(EMA50>EMA200) 상승추세에서 EMA20 까지 눌렸다가 반등 확인 봉
  macd      : MACD 가 0선 아래에서 시그널선을 상향 돌파 + 종가가 EMA200 위 (숏은 대칭)
  bollinger : 볼린저 하단 밖으로 나갔다가 다시 안으로 들어오며 RSI 과매도 → 중심선까지 반등 노림
  rsi2      : 래리 코너스 RSI(2) - EMA200 위에서 RSI(2)<10 극단 과매도 매수
  heikin    : 하이킨아시 음봉 3개 이상 후 아래꼬리 없는 강한 양봉 전환 + EMA200 추세 방향
  box       : 박스권 돌파 - 최근 20봉 고저폭이 ATR 4배 이하(횡보)였다가 거래량 1.5배 이상 동반 돌파.
              손절 = 박스 중간, 익절 = 박스 높이만큼(측정 이동)
"""
import numpy as np
import pandas as pd

ENGINES = ["pullback", "macd", "bollinger", "rsi2", "heikin", "box"]


def ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(s, n):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def heikin_ashi(f):
    ha_close = (f["open"] + f["high"] + f["low"] + f["close"]) / 4
    o = np.empty(len(f))
    o[0] = (f["open"].iloc[0] + f["close"].iloc[0]) / 2
    hc = ha_close.to_numpy()
    for i in range(1, len(f)):
        o[i] = (o[i - 1] + hc[i - 1]) / 2
    ha_open = pd.Series(o, index=f.index)
    ha_high = pd.concat([f["high"], ha_open, ha_close], axis=1).max(axis=1)
    ha_low = pd.concat([f["low"], ha_open, ha_close], axis=1).min(axis=1)
    return ha_open, ha_high, ha_low, ha_close


def add_signals(f: pd.DataFrame, engine: str, put) -> None:
    c, h, l = f["close"], f["high"], f["low"]
    a = f["atr"]
    e20, e50, e200 = ema(c, 20), ema(c, 50), ema(c, 200)
    up, dn = (e50 > e200) & (c > e200), (e50 < e200) & (c < e200)

    if engine == "pullback":
        r = rsi(c, 14)
        dipped = (l <= e20) | (l.shift(1) <= e20.shift(1))
        long_ = up & dipped & (c > e20) & (c > h.shift(1)) & (r.rolling(3).min() < 50)
        popped = (h >= e20) | (h.shift(1) >= e20.shift(1))
        short = dn & popped & (c < e20) & (c < l.shift(1)) & (r.rolling(3).max() > 50)
        put(long_, 1, "PULLBACK", 1.5 * a, 2.5 * a)
        put(short, -1, "PULLBACK", 1.5 * a, 2.5 * a)

    elif engine == "macd":
        macd = ema(c, 12) - ema(c, 26)
        sigl = macd.ewm(span=9, adjust=False).mean()
        cross_up = (macd > sigl) & (macd.shift(1) <= sigl.shift(1)) & (macd < 0)
        cross_dn = (macd < sigl) & (macd.shift(1) >= sigl.shift(1)) & (macd > 0)
        put(cross_up & (c > e200), 1, "MACD", 1.5 * a, 3.0 * a)
        put(cross_dn & (c < e200), -1, "MACD", 1.5 * a, 3.0 * a)

    elif engine == "bollinger":
        mid = c.rolling(20).mean()
        sd = c.rolling(20).std()
        lo, hi = mid - 2 * sd, mid + 2 * sd
        r = rsi(c, 14)
        long_ = (c.shift(1) < lo.shift(1)) & (c > lo) & (r.shift(1) < 35)
        short = (c.shift(1) > hi.shift(1)) & (c < hi) & (r.shift(1) > 65)
        put(long_, 1, "BOLLINGER", 1.5 * a, (mid - c).clip(lower=0))
        put(short, -1, "BOLLINGER", 1.5 * a, (c - mid).clip(lower=0))

    elif engine == "rsi2":
        r2 = rsi(c, 2)
        put((c > e200) & (r2 < 10), 1, "RSI2", 2.0 * a, 1.5 * a)
        put((c < e200) & (r2 > 90), -1, "RSI2", 2.0 * a, 1.5 * a)

    elif engine == "heikin":
        ho, hh, hl, hc = heikin_ashi(f)
        bull, bear = hc > ho, hc < ho
        red3 = bear.shift(1) & bear.shift(2) & bear.shift(3)
        green3 = bull.shift(1) & bull.shift(2) & bull.shift(3)
        tol = 0.05 * a
        strong_bull = bull & ((ho - hl) <= tol)   # 아래꼬리 없음
        strong_bear = bear & ((hh - ho) <= tol)   # 위꼬리 없음
        put(red3 & strong_bull & (c > e200), 1, "HEIKIN", 1.5 * a, 3.0 * a)
        put(green3 & strong_bear & (c < e200), -1, "HEIKIN", 1.5 * a, 3.0 * a)

    elif engine == "box":
        n = 20
        top = h.rolling(n).max().shift(1)
        bot = l.rolling(n).min().shift(1)
        height = top - bot
        boxed = height <= 4.0 * a.shift(1)                      # 직전 20봉이 좁은 범위(박스)
        vol_ok = f["volume"] > 1.5 * f["volume"].rolling(n).mean().shift(1)
        mid = (top + bot) / 2
        long_ = boxed & vol_ok & (c > top) & (c.shift(1) <= top)
        short = boxed & vol_ok & (c < bot) & (c.shift(1) >= bot)
        put(long_, 1, "BOX", (c - mid).clip(lower=0.5 * a), height)
        put(short, -1, "BOX", (mid - c).clip(lower=0.5 * a), height)

    else:
        raise ValueError(f"알 수 없는 engine: {engine} (가능: orderflow, {', '.join(ENGINES)})")
