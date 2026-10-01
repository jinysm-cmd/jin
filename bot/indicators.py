"""오더플로우 기반 지표 모음.

바이낸스 kline 에는 '테이커 매수 체결량(taker buy base volume)'이 들어있다.
이걸로 캔들마다 공격적 매수/매도 비율(델타)을 계산할 수 있어서,
호가창 데이터 없이도 간단한 오더플로우 분석이 가능하다.
"""
import numpy as np
import pandas as pd


def rolling_vwap(df: pd.DataFrame, n: int) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = (tp * df["volume"]).rolling(n).sum()
    v = df["volume"].rolling(n).sum()
    return pv / v.replace(0, np.nan)


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def taker_imbalance(df: pd.DataFrame) -> pd.Series:
    """(테이커매수 - 테이커매도) / 전체거래량. -1(전부 시장가 매도) ~ +1(전부 시장가 매수)."""
    vol = df["volume"].replace(0, np.nan)
    return (2.0 * df["taker_buy_volume"] - df["volume"]) / vol


def close_location(df: pd.DataFrame) -> pd.Series:
    """캔들 범위 내 종가 위치. 0=저가 마감, 1=고가 마감."""
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    return ((df["close"] - df["low"]) / rng).fillna(0.5)


def log_volume_z(df: pd.DataFrame, n: int) -> pd.Series:
    lv = np.log1p(df["volume"])
    mu = lv.rolling(n).mean()
    sd = lv.rolling(n).std()
    return (lv - mu) / sd.replace(0, np.nan)


def rolling_pct_rank(s: pd.Series, n: int) -> pd.Series:
    """현재 값이 최근 n개 중 몇 % 위치인지 (0~1)."""
    from numpy.lib.stride_tricks import sliding_window_view

    v = s.to_numpy(dtype=float)
    out = np.full(len(v), np.nan)
    if len(v) >= n:
        w = sliding_window_view(v, n)
        r = (w[:, :-1] < w[:, -1:]).mean(axis=1)
        r[np.isnan(w).any(axis=1)] = np.nan
        out[n - 1:] = r
    return pd.Series(out, index=s.index)
