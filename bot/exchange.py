"""바이낸스 USDⓈ-M 선물 REST 클라이언트 (외부 라이브러리 없이 requests 만 사용)."""
import hashlib
import hmac
import logging
import math
import time
from urllib.parse import urlencode

import pandas as pd
import requests

log = logging.getLogger(__name__)

MAINNET = "https://fapi.binance.com"
TESTNET = "https://testnet.binancefuture.com"

KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_volume", "taker_buy_quote_volume", "ignore",
]

INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
}


class BinanceError(Exception):
    def __init__(self, status, code, msg):
        super().__init__(f"HTTP {status} code={code} {msg}")
        self.status, self.code, self.msg = status, code, msg


class BinanceFutures:
    def __init__(self, api_key: str = "", api_secret: str = "", base_url: str = MAINNET, recv_window: int = 5000):
        self.key = api_key
        self.secret = api_secret.encode() if api_secret else b""
        self.base = base_url.rstrip("/")
        self.recv_window = recv_window
        self.s = requests.Session()
        if api_key:
            self.s.headers["X-MBX-APIKEY"] = api_key
        self.time_offset = 0
        self._filters: dict[str, dict] = {}

    # ---------------------------------------------------------------- 저수준
    def _request(self, method: str, path: str, params: dict | None = None, signed: bool = False, retries: int = 3):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        for attempt in range(retries):
            q = dict(params)
            if signed:
                q["timestamp"] = int(time.time() * 1000) + self.time_offset
                q["recvWindow"] = self.recv_window
                qs = urlencode(q)
                q_str = qs + "&signature=" + hmac.new(self.secret, qs.encode(), hashlib.sha256).hexdigest()
            else:
                q_str = urlencode(q)
            url = f"{self.base}{path}" + (f"?{q_str}" if q_str else "")
            try:
                r = self.s.request(method, url, timeout=15)
            except requests.RequestException as e:
                log.warning("네트워크 오류 %s %s: %s (재시도 %d)", method, path, e, attempt + 1)
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code in (418, 429):
                wait = int(r.headers.get("Retry-After", "5"))
                log.warning("레이트리밋 %s, %ds 대기", r.status_code, wait)
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
            data = r.json() if r.text else {}
            if r.status_code >= 400:
                code = data.get("code") if isinstance(data, dict) else None
                msg = data.get("msg") if isinstance(data, dict) else r.text
                if code == -1021 and attempt < retries - 1:  # 타임스탬프 오차
                    self.sync_time()
                    continue
                raise BinanceError(r.status_code, code, msg)
            return data
        raise BinanceError(0, None, f"{method} {path} 재시도 초과")

    def sync_time(self):
        server = self._request("GET", "/fapi/v1/time")["serverTime"]
        self.time_offset = server - int(time.time() * 1000)

    # ---------------------------------------------------------------- 시세
    def klines(self, symbol: str, interval: str, limit: int = 500, start: int | None = None, end: int | None = None) -> pd.DataFrame:
        raw = self._request("GET", "/fapi/v1/klines", {
            "symbol": symbol, "interval": interval, "limit": limit, "startTime": start, "endTime": end,
        })
        return klines_to_df(raw)

    def klines_range(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        step = INTERVAL_MS[interval]
        frames, cur = [], start_ms
        while cur < end_ms:
            df = self.klines(symbol, interval, limit=1500, start=cur, end=end_ms)
            if df.empty:
                break
            frames.append(df)
            cur = int(df.index[-1].value // 1_000_000) + step
            time.sleep(0.15)
        if not frames:
            return klines_to_df([])
        out = pd.concat(frames)
        return out[~out.index.duplicated()].sort_index()

    def funding_history(self, symbol: str, start_ms: int, end_ms: int) -> pd.Series:
        rows, cur = [], start_ms
        while cur < end_ms:
            data = self._request("GET", "/fapi/v1/fundingRate", {
                "symbol": symbol, "startTime": cur, "endTime": end_ms, "limit": 1000,
            })
            if not data:
                break
            rows.extend(data)
            cur = data[-1]["fundingTime"] + 1
            if len(data) < 1000:
                break
            time.sleep(0.15)
        if not rows:
            return pd.Series(dtype=float)
        s = pd.Series(
            [float(r["fundingRate"]) for r in rows],
            index=pd.to_datetime([r["fundingTime"] for r in rows], unit="ms", utc=True),
        )
        return s[~s.index.duplicated()].sort_index()

    def premium_index(self, symbol: str | None = None):
        return self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})

    def ticker_24h(self):
        return self._request("GET", "/fapi/v1/ticker/24hr")

    def exchange_info(self):
        return self._request("GET", "/fapi/v1/exchangeInfo")

    def load_filters(self):
        info = self.exchange_info()
        for s in info["symbols"]:
            flt = {f["filterType"]: f for f in s["filters"]}
            self._filters[s["symbol"]] = {
                "status": s.get("status"),
                "contractType": s.get("contractType"),
                "quoteAsset": s.get("quoteAsset"),
                "tick": float(flt["PRICE_FILTER"]["tickSize"]),
                "step": float(flt.get("MARKET_LOT_SIZE", flt["LOT_SIZE"])["stepSize"]),
                "min_qty": float(flt.get("MARKET_LOT_SIZE", flt["LOT_SIZE"])["minQty"]),
                "max_qty": float(flt.get("MARKET_LOT_SIZE", flt["LOT_SIZE"])["maxQty"]),
                "min_notional": float(flt.get("MIN_NOTIONAL", {}).get("notional", 5.0)),
            }
        return self._filters

    def filters(self, symbol: str) -> dict:
        if not self._filters:
            self.load_filters()
        return self._filters[symbol]

    def round_qty(self, symbol: str, qty: float) -> float:
        f = self.filters(symbol)
        q = math.floor(qty / f["step"] + 1e-9) * f["step"]
        return float(f"{min(q, f['max_qty']):.10f}")

    def round_price(self, symbol: str, price: float) -> float:
        f = self.filters(symbol)
        p = round(price / f["tick"]) * f["tick"]
        return float(f"{p:.10f}")

    # ---------------------------------------------------------------- 계정/주문
    def balance_usdt(self) -> float:
        for b in self._request("GET", "/fapi/v2/balance", signed=True):
            if b["asset"] == "USDT":
                return float(b["balance"])
        return 0.0

    def positions(self) -> dict[str, dict]:
        out = {}
        for p in self._request("GET", "/fapi/v2/positionRisk", signed=True):
            amt = float(p["positionAmt"])
            if amt != 0:
                out[p["symbol"]] = {"amt": amt, "entry": float(p["entryPrice"]), "mark": float(p["markPrice"])}
        return out

    def set_leverage(self, symbol: str, leverage: int):
        return self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, signed=True)

    def is_hedge_mode(self) -> bool:
        return bool(self._request("GET", "/fapi/v1/positionSide/dual", signed=True).get("dualSidePosition"))

    def set_one_way_mode(self):
        self._request("POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": "false"}, signed=True)

    def set_isolated(self, symbol: str):
        try:
            self._request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"}, signed=True)
        except BinanceError as e:
            if e.code != -4046:  # 이미 ISOLATED
                raise

    def market_order(self, symbol: str, side: str, qty: float, reduce_only: bool = False):
        return self._request("POST", "/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": "MARKET", "quantity": qty,
            "reduceOnly": "true" if reduce_only else None, "newOrderRespType": "RESULT",
        }, signed=True)

    def place_protective_stop(self, symbol: str, close_side: str, stop_price: float):
        """거래소 측 비상 손절(closePosition). 봇이 죽어도 포지션이 보호되도록 하는 안전망.

        바이낸스가 조건부 주문을 Algo Order API 로 옮기는 중이라, 일반 주문 엔드포인트가
        거부하면(-4120) algoOrder 엔드포인트로 재시도한다.

        반환값: 취소할 때 쓰는 참조 {"kind": "algo"|"order", "id": ...}
        """
        sp = self.round_price(symbol, stop_price)
        if not getattr(self, "_algo_only", False):
            try:
                r = self._request("POST", "/fapi/v1/order", {
                    "symbol": symbol, "side": close_side, "type": "STOP_MARKET", "stopPrice": sp,
                    "closePosition": "true", "workingType": "MARK_PRICE",
                }, signed=True)
                return {"kind": "order", "id": r.get("orderId")}
            except BinanceError as e:
                if e.code != -4120:
                    raise
                log.info("이 계정은 손절 주문에 Algo Order API 를 사용합니다.")
                self._algo_only = True
        r = self._request("POST", "/fapi/v1/algoOrder", {
            "algoType": "CONDITIONAL", "symbol": symbol, "side": close_side, "type": "STOP_MARKET",
            "triggerPrice": sp, "closePosition": "true", "workingType": "MARK_PRICE",
        }, signed=True)
        return {"kind": "algo", "id": r.get("algoId")}

    def cancel_protective_stop(self, symbol: str, ref: dict | None) -> bool:
        """등록해 둔 비상 손절 주문 하나를 ID 로 취소. 이미 체결/취소된 경우도 True."""
        if not ref or ref.get("id") is None:
            return False
        try:
            if ref["kind"] == "algo":
                self._request("DELETE", "/fapi/v1/algoOrder", {"algoId": ref["id"]}, signed=True)
            else:
                self._request("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": ref["id"]}, signed=True)
            return True
        except BinanceError as e:
            if e.code in (-2011, -2013):  # 주문 없음(이미 체결/취소)
                return True
            log.warning("%s 비상손절 주문(%s) 취소 실패: %s", symbol, ref, e)
            return False

    def cancel_all(self, symbol: str):
        for path in ("/fapi/v1/allOpenOrders", "/fapi/v1/algoOpenOrders"):
            try:
                self._request("DELETE", path, {"symbol": symbol}, signed=True)
            except BinanceError as e:
                log.debug("cancel_all %s %s: %s", symbol, path, e)


def klines_to_df(raw) -> pd.DataFrame:
    df = pd.DataFrame(raw, columns=KLINE_COLS)
    for c in ("open", "high", "low", "close", "volume", "quote_volume", "taker_buy_volume", "taker_buy_quote_volume"):
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"].astype("int64"), unit="ms", utc=True)
    return df.set_index("open_time")[["open", "high", "low", "close", "volume", "taker_buy_volume", "close_time"]]
