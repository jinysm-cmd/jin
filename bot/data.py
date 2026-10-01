"""캔들 데이터 다운로드/캐시 및 거래 대상 심볼 선정."""
import glob
import logging
import os
import time

import pandas as pd

from .exchange import BinanceFutures, INTERVAL_MS

log = logging.getLogger(__name__)

DEFAULT_EXCLUDE = {"USDCUSDT", "FDUSDUSDT", "BTCDOMUSDT"}


def top_symbols(client: BinanceFutures, top_n: int, min_quote_volume: float, exclude=()) -> list[str]:
    """24시간 거래대금 상위 USDT 무기한 선물."""
    filters = client.load_filters()
    excl = DEFAULT_EXCLUDE | set(exclude or ())
    rows = []
    for t in client.ticker_24h():
        s = t["symbol"]
        f = filters.get(s)
        if not f or f["status"] != "TRADING" or f["contractType"] != "PERPETUAL" or f["quoteAsset"] != "USDT":
            continue
        if s in excl or not s.isascii():
            continue
        qv = float(t["quoteVolume"])
        if qv >= min_quote_volume:
            rows.append((qv, s))
    rows.sort(reverse=True)
    return [s for _, s in rows[:top_n]]


def load_klines(client: BinanceFutures, symbol: str, interval: str, days: int, cache_dir: str = "data") -> pd.DataFrame:
    """CSV 캐시가 있으면 이어서 받고, 없으면 새로 받는다."""
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{symbol}_{interval}.csv")
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000
    cached = None
    if os.path.exists(path):
        cached = pd.read_csv(path, index_col=0, parse_dates=["close_time"])
        cached.index = pd.to_datetime(cached.index, utc=True)
        if len(cached) and cached.index[0].value // 1_000_000 <= start_ms + INTERVAL_MS[interval]:
            start_ms = int(cached.index[-1].value // 1_000_000) + INTERVAL_MS[interval]
        else:
            cached = None
    log.info("%s %s 다운로드 중...", symbol, interval)
    new = client.klines_range(symbol, interval, start_ms, end_ms)
    df = pd.concat([cached, new]) if cached is not None else new
    df = df[~df.index.duplicated()].sort_index()
    # 아직 마감되지 않은 마지막 봉 제거
    now = pd.Timestamp.now(tz="UTC")
    df = df[df["close_time"] < now]
    df.to_csv(path)
    cutoff = pd.Timestamp(end_ms - days * 86_400_000, unit="ms", tz="UTC")
    return df[df.index >= cutoff]


def load_vision_dir(path: str, symbol: str, interval: str) -> pd.DataFrame:
    """data.binance.vision 에서 받은 월별 CSV(압축 해제)들을 읽는다.

    예) data/vision/BTCUSDT-5m-2026-01.csv ...
    """
    files = sorted(glob.glob(os.path.join(path, f"{symbol}-{interval}-*.csv")))
    frames = []
    for fp in files:
        with open(fp) as fh:
            has_header = not fh.readline()[:1].isdigit()
        df = pd.read_csv(fp, header=0 if has_header else None)
        df = df.iloc[:, :11]
        df.columns = ["open_time", "open", "high", "low", "close", "volume", "close_time",
                      "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote_volume"]
        frames.append(df)
    if not frames:
        raise FileNotFoundError(f"{path} 에 {symbol}-{interval}-*.csv 파일이 없습니다")
    df = pd.concat(frames)
    df["open_time"] = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"].astype("int64"), unit="ms", utc=True)
    df = df.set_index("open_time")[["open", "high", "low", "close", "volume", "taker_buy_volume", "close_time"]]
    return df[~df.index.duplicated()].sort_index().astype(
        {c: float for c in ["open", "high", "low", "close", "volume", "taker_buy_volume"]})


def read_cache(symbol: str, interval: str, cache_dir: str = "data") -> pd.DataFrame:
    """backtest.py 가 받아둔 CSV 캐시를 네트워크 없이 읽는다."""
    df = pd.read_csv(os.path.join(cache_dir, f"{symbol}_{interval}.csv"), index_col=0, parse_dates=["close_time"])
    df.index = pd.to_datetime(df.index, utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], utc=True)
    return df


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """5분봉을 15분/1시간봉 등으로 합친다 (테이커 매수량도 합산)."""
    out = df.resample(rule, label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
        "volume": "sum", "taker_buy_volume": "sum", "close_time": "last",
    })
    return out.dropna(subset=["open", "close"])
