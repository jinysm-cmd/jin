"""네이버 금융에서 종목 목록과 일봉(수정주가)을 받아 CSV 로 캐시한다."""
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

log = logging.getLogger(__name__)

LIST_URL = "https://m.stock.naver.com/api/stocks/marketValue/{market}?page={page}&pageSize=100"
CHART_URL = "https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count={count}&requestType=0"
HEADERS = {"User-Agent": "Mozilla/5.0"}
EXCLUDE_WORDS = ("스팩", "리츠", "ETN", "인프라")


def _get(url: str, tries: int = 4) -> requests.Response:
    for i in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(1.5 * (i + 1))


def list_stocks(cache_dir: str = "data/krx") -> pd.DataFrame:
    """코스피/코스닥 보통주 목록 (ETF, 우선주, 스팩, 리츠 제외)."""
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, "universe.csv")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 86_400:
        return pd.read_csv(path, dtype={"code": str})
    rows = []
    for market in ("KOSPI", "KOSDAQ"):
        page = 1
        while True:
            d = _get(LIST_URL.format(market=market, page=page)).json()
            for s in d["stocks"]:
                name, code = s["stockName"], s["itemCode"]
                if s["stockEndType"] != "stock" or not code.isdigit() or code[-1] != "0":
                    continue
                if any(w in name for w in EXCLUDE_WORDS):
                    continue
                rows.append({"code": code, "name": name, "market": market,
                             "market_value": float(s["marketValueRaw"] or 0)})
            if page * 100 >= d["totalCount"]:
                break
            page += 1
    df = pd.DataFrame(rows).drop_duplicates("code")
    df.to_csv(path, index=False)
    return df


def fetch_daily(code: str, count: int = 2600) -> pd.DataFrame:
    text = _get(CHART_URL.format(code=code, count=count)).content.decode("euc-kr", "replace")
    recs = []
    for line in text.split('data="')[1:]:
        f = line.split('"', 1)[0].split("|")
        if len(f) == 6 and all(f[1:]):
            recs.append(f)
    df = pd.DataFrame(recs, columns=["date", "open", "high", "low", "close", "volume"])
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    df = df.set_index("date").astype(float)
    return df[(df["volume"] > 0) & (df["open"] > 0)]


def load_all(codes, cache_dir: str = "data/krx", refresh: bool = False, workers: int = 8) -> dict:
    """{code: DataFrame}. 캐시가 오늘 것이면 그대로 쓴다."""
    os.makedirs(os.path.join(cache_dir, "daily"), exist_ok=True)

    def one(code):
        path = os.path.join(cache_dir, "daily", f"{code}.csv")
        fresh = os.path.exists(path) and time.time() - os.path.getmtime(path) < 20 * 3600
        if fresh and not refresh:
            return code, pd.read_csv(path, index_col=0, parse_dates=True)
        try:
            df = fetch_daily(code)
        except Exception as e:  # noqa: BLE001
            log.warning("%s 다운로드 실패: %s", code, e)
            return code, None
        df.to_csv(path)
        return code, df

    out = {}
    with ThreadPoolExecutor(workers) as ex:
        for i, (code, df) in enumerate(ex.map(one, codes), 1):
            if df is not None and len(df) > 0:
                out[code] = df
            if i % 200 == 0:
                log.info("%d/%d 종목 로드", i, len(codes))
    return out
