"""네이버 모바일 API 에서 종목별 투자자(외국인/기관/개인) 일별 순매수 수량을 받아 캐시한다.

수량은 그 당시 주식 수 기준이라 액면분할 전후로 단위가 다르다.
그래서 같은 날 거래량으로 나눈 비율(순매수/거래량)로만 쓴다.
"""
import logging
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

log = logging.getLogger(__name__)
URL = "https://m.stock.naver.com/api/stock/{code}/trend?pageSize=60&bizdate={bizdate}"
START = pd.Timestamp("2016-01-01")


_local = threading.local()


def _get_json(url: str, tries: int = 6):
    """스레드마다 연결을 유지한다 (새 연결을 자주 열면 끊김)."""
    for i in range(tries):
        if not hasattr(_local, "s"):
            _local.s = requests.Session()
            _local.s.headers["User-Agent"] = "Mozilla/5.0"
        try:
            r = _local.s.get(url, timeout=15)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError):
            del _local.s
            if i == tries - 1:
                raise
            time.sleep(1 + i)


def _num(s):
    if s is None:
        return float("nan")
    s = str(s).replace(",", "").replace("+", "").strip()
    try:
        return float(s)
    except ValueError:
        return float("nan")


def fetch_flow(code: str, start: pd.Timestamp = START) -> pd.DataFrame:
    rows = []
    bizdate = (pd.Timestamp.today() + pd.Timedelta(days=1)).strftime("%Y%m%d")
    seen = set()
    while True:
        try:
            d = _get_json(URL.format(code=code, bizdate=bizdate))
        except Exception:  # noqa: BLE001  상장일 이전 페이지는 오류를 줄 수 있음
            if rows:
                break
            raise
        if not d:
            break
        for r in d:
            if r["bizdate"] in seen:
                continue
            seen.add(r["bizdate"])
            rows.append({
                "date": r["bizdate"], "foreign": _num(r.get("foreignerPureBuyQuant")),
                "organ": _num(r.get("organPureBuyQuant")), "indiv": _num(r.get("individualPureBuyQuant")),
                "volume": _num(r.get("accumulatedTradingVolume")),
                "foreign_ratio": _num(str(r.get("foreignerHoldRatio", "")).replace("%", "")),
            })
        last = pd.Timestamp(d[-1]["bizdate"])
        if last <= start or len(d) < 60:
            break
        bizdate = d[-1]["bizdate"]
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    return df.set_index("date").sort_index()


def load_flows(codes, cache_dir: str = "data/krx/flow", workers: int = 6, max_age_h: float = 20) -> dict:
    os.makedirs(cache_dir, exist_ok=True)

    def one(code):
        path = os.path.join(cache_dir, f"{code}.csv")
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age_h * 3600:
            return code, pd.read_csv(path, index_col=0, parse_dates=True)
        try:
            df = fetch_flow(code)
        except Exception as e:  # noqa: BLE001
            log.warning("%s 수급 다운로드 실패: %s", code, e)
            return code, None
        df.to_csv(path)
        return code, df

    out = {}
    with ThreadPoolExecutor(workers) as ex:
        for i, (code, df) in enumerate(ex.map(one, codes), 1):
            if df is not None and len(df):
                out[code] = df
            if i % 25 == 0:
                log.warning("수급 %d/%d", i, len(codes))
    return out
