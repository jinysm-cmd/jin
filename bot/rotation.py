"""코인 순환매(롱숏 모멘텀) 실행 로직.

매 hold_days 일마다(UTC 00:05, 일봉 마감 직후):
  1) 24h 거래대금 min_quote_volume 이상인 USDT 무기한 선물을 후보로
  2) 최근 30일 평균 거래대금 상위 universe_size 개 중 상장 min_age_days 일 이상만 남기고
  3) 지난 lookback_days 일 수익률로 줄 세워 상위 top_k 롱 / 하위 top_k 숏 (같은 금액)
  4) 각 포지션에는 거래소 비상손절(진입가 대비 catastrophe_stop) 을 건다 (숏 스퀴즈 대비)
백테스트(momentum_lab.py [B])와 같은 규칙. 다른 봇/수동 포지션이 있는 코인은 건너뛴다.
"""
import json
import logging
import os
import time
from datetime import datetime, timezone

import pandas as pd

from .exchange import BinanceError, BinanceFutures, MAINNET

log = logging.getLogger(__name__)

DEFAULTS = {
    "lookback_days": 14, "hold_days": 14, "top_k": 5, "universe_size": 30, "min_age_days": 60,
    "min_quote_volume": 5_000_000, "exposure": 1.0, "leverage": 2, "catastrophe_stop": 0.30,
    "capital_usdt": 0, "paper_start_equity": 350, "fee": 0.0005, "slippage": 0.0002,
    "exclude": ["USDCUSDT", "FDUSDUSDT", "BTCDOMUSDT", "PAXGUSDT", "XAUTUSDT", "USDPUSDT", "TUSDUSDT"],
}


def rank_symbols(daily: dict, cfg: dict) -> pd.Series:
    """daily: {심볼: 마감된 일봉 DataFrame(close, volume)} → 모멘텀 점수(오름차순)."""
    rows = {}
    for s, df in daily.items():
        if len(df) < max(cfg["min_age_days"], cfg["lookback_days"] + 1, 30):
            continue
        qv30 = (df["close"] * df["volume"]).iloc[-30:].mean()
        mom = df["close"].iloc[-1] / df["close"].iloc[-1 - cfg["lookback_days"]] - 1
        rows[s] = (qv30, mom)
    if not rows:
        return pd.Series(dtype=float)
    t = pd.DataFrame(rows, index=["qv30", "mom"]).T
    t = t.sort_values("qv30", ascending=False).head(cfg["universe_size"])
    return t["mom"].sort_values()


def pick_targets(ranked: pd.Series, k: int, allowed) -> dict:
    """하위 k 숏(-1), 상위 k 롱(+1). allowed(sym) 가 False 면 다음 순위로 넘어간다."""
    out = {}
    longs = [s for s in reversed(ranked.index) if allowed(s)][:k]
    shorts = [s for s in ranked.index if allowed(s) and s not in longs][:k]
    if len(longs) < k or len(shorts) < k:   # 롱숏 균형이 깨지면 쉬어 간다
        return {}
    out.update({s: 1 for s in longs})
    out.update({s: -1 for s in shorts})
    return out


class Rotator:
    def __init__(self, cfg: dict, mode: str):
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.mode = mode
        self.state_path = f"rotation_state_{mode}.json"
        if mode == "paper":
            self.c = BinanceFutures(base_url=MAINNET)
        else:
            key, sec = os.environ.get("BINANCE_API_KEY", ""), os.environ.get("BINANCE_API_SECRET", "")
            if not key or not sec:
                raise SystemExit("환경변수 BINANCE_API_KEY / BINANCE_API_SECRET 를 설정하세요")
            self.c = BinanceFutures(key, sec, base_url=MAINNET)
            if self.c.is_hedge_mode():
                raise SystemExit("계정이 헤지 모드입니다. 단방향(One-way) 모드로 바꾼 뒤 실행하세요.")
        self.c.sync_time()
        self.c.load_filters()
        self.state = self._load()

    # ------------------------------------------------------------ 상태
    def _load(self):
        if os.path.exists(self.state_path):
            with open(self.state_path) as fh:
                return json.load(fh)
        return {"positions": {}, "last_rebalance": None, "equity": self.cfg["paper_start_equity"], "history": []}

    def _save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.state, fh, indent=1, default=str)
        os.replace(tmp, self.state_path)

    # ------------------------------------------------------------ 데이터
    def _daily(self) -> dict:
        excl = set(self.cfg["exclude"])
        f = self.c.load_filters()
        cands = []
        for t in self.c.ticker_24h():
            s = t["symbol"]
            fi = f.get(s)
            if not fi or fi["status"] != "TRADING" or fi["contractType"] != "PERPETUAL" or fi["quoteAsset"] != "USDT":
                continue
            if s in excl or not s.isascii() or float(t["quoteVolume"]) < self.cfg["min_quote_volume"]:
                continue
            cands.append(s)
        now = pd.Timestamp.now(tz="UTC")
        out = {}
        for s in cands:
            try:
                df = self.c.klines(s, "1d", limit=100)
            except BinanceError as e:
                log.warning("%s 일봉 조회 실패: %s", s, e)
                continue
            out[s] = df[df["close_time"] < now]   # 마감된 봉만
            time.sleep(0.05)
        return out

    def _prices(self) -> dict:
        return {p["symbol"]: float(p["markPrice"]) for p in self.c.premium_index()}

    # ------------------------------------------------------------ 평가
    def equity(self, prices: dict) -> float:
        if self.mode == "paper":
            upnl = sum(p["side"] * p["qty"] * (prices.get(s, p["entry"]) - p["entry"])
                       for s, p in self.state["positions"].items())
            return self.state["equity"] + upnl
        ex = self.c.positions()
        return self.c.balance_usdt() + sum(v["amt"] * (v["mark"] - v["entry"]) for v in ex.values())

    def capital(self, prices: dict) -> float:
        eq = self.equity(prices)
        cap = float(self.cfg.get("capital_usdt") or 0)
        return min(cap, eq) if cap > 0 else eq

    # ------------------------------------------------------------ 주문
    def _close(self, sym: str, pos: dict, px: float, ex_pos: dict | None):
        cost = self.cfg["fee"] + self.cfg["slippage"]
        if self.mode == "paper":
            pnl = pos["side"] * pos["qty"] * (px - pos["entry"]) - pos["qty"] * px * cost
            self.state["equity"] += pnl
        else:
            amt = abs((ex_pos or {}).get(sym, {}).get("amt", 0))
            if amt > 0:
                r = self.c.market_order(sym, "SELL" if pos["side"] == 1 else "BUY", amt, reduce_only=True)
                px = float(r.get("avgPrice") or 0) or px
            self.c.cancel_protective_stop(sym, pos.get("stop_ref"))
            self.c.cancel_all(sym)
            pnl = pos["side"] * pos["qty"] * (px - pos["entry"])
        log.info("청산 %s %s @%.6g  손익(수수료 전 추정)=%.2f USDT", "롱" if pos["side"] == 1 else "숏", sym, px, pnl)
        del self.state["positions"][sym]

    def _open(self, sym: str, side: int, notional: float, px: float):
        qty = self.c.round_qty(sym, notional / px)
        if qty <= 0 or qty * px < self.c.filters(sym)["min_notional"]:
            log.info("%s 최소 주문금액 미달로 건너뜀", sym)
            return
        stop_px = self.c.round_price(sym, px * (1 - side * self.cfg["catastrophe_stop"]))
        ref = None
        if self.mode == "paper":
            self.state["equity"] -= qty * px * (self.cfg["fee"] + self.cfg["slippage"])
        else:
            try:
                self.c.set_isolated(sym)
            except BinanceError as e:
                log.warning("%s 격리 마진 설정 실패, 현재 마진 방식으로 진행: %s", sym, e)
            self.c.set_leverage(sym, int(self.cfg["leverage"]))
            self.c.cancel_all(sym)
            r = self.c.market_order(sym, "BUY" if side == 1 else "SELL", qty)
            px = float(r.get("avgPrice") or 0) or px
            stop_px = self.c.round_price(sym, px * (1 - side * self.cfg["catastrophe_stop"]))
            try:
                ref = self.c.place_protective_stop(sym, "SELL" if side == 1 else "BUY", stop_px)
            except BinanceError as e:
                log.warning("%s 비상손절 등록 실패: %s", sym, e)
        self.state["positions"][sym] = {"side": side, "qty": qty, "entry": px, "stop": stop_px,
                                        "stop_ref": ref, "opened": datetime.now(timezone.utc).isoformat()}
        log.info("진입 %s %s qty=%s @%.6g (%.1f USDT, 비상손절 %.6g)",
                 "롱" if side == 1 else "숏", sym, qty, px, qty * px, stop_px)

    # ------------------------------------------------------------ 동작
    def check_stops(self, prices: dict):
        """비상손절에 걸린 포지션 정리 (paper 는 가격으로 판정, live 는 거래소에서 사라졌는지로 판정)."""
        ex = self.c.positions() if self.mode != "paper" else None
        for sym, pos in list(self.state["positions"].items()):
            px = prices.get(sym)
            if self.mode == "paper":
                if px and (px - pos["stop"]) * pos["side"] <= 0:
                    log.warning("%s 비상손절 도달", sym)
                    self._close(sym, pos, pos["stop"], None)
            elif sym not in ex:
                log.warning("%s 포지션이 거래소에서 이미 청산됨(비상손절/수동). 상태에서 제거.", sym)
                self.c.cancel_all(sym)
                del self.state["positions"][sym]
        self._save()

    def due(self, now: datetime) -> bool:
        last = self.state.get("last_rebalance")
        if not last:
            return True
        return (now.date() - datetime.fromisoformat(last).date()).days >= self.cfg["hold_days"] and \
            now.hour * 60 + now.minute >= 5

    def rebalance(self):
        log.info("===== 리밸런스 시작 =====")
        daily = self._daily()
        ranked = rank_symbols(daily, self.cfg)
        prices = self._prices()
        ex = self.c.positions() if self.mode != "paper" else {}
        mine = set(self.state["positions"])
        cap = self.capital(prices)
        notional = cap * self.cfg["exposure"] / 2 / self.cfg["top_k"]

        def allowed(s):  # 시세 있음 + 다른 봇/수동 포지션 없음 + 최소 주문금액 충족
            return s in prices and (s not in ex or s in mine) and self.c.filters(s)["min_notional"] <= notional
        targets = pick_targets(ranked, self.cfg["top_k"], allowed)
        if not targets:
            log.warning("롱/숏 대상이 부족해 이번 회차는 전부 정리하고 쉽니다.")
        log.info("롱: %s", ", ".join(f"{s}({ranked[s]:+.0%})" for s, d in targets.items() if d == 1))
        log.info("숏: %s", ", ".join(f"{s}({ranked[s]:+.0%})" for s, d in targets.items() if d == -1))
        for sym, pos in list(self.state["positions"].items()):
            if targets.get(sym) != pos["side"]:
                self._close(sym, pos, prices.get(sym, pos["entry"]), ex)
        for sym, side in targets.items():
            if sym not in self.state["positions"]:
                try:
                    self._open(sym, side, notional, prices[sym])
                except BinanceError as e:
                    log.error("%s 진입 실패: %s", sym, e)
        self.state["last_rebalance"] = datetime.now(timezone.utc).isoformat()
        self._save()
        log.info("리밸런스 완료. 포지션 %d개, 1개당 약 %.1f USDT, 평가자산 %.2f USDT",
                 len(self.state["positions"]), notional, self.equity(prices))

    def run(self):
        log.info("순환매 봇 시작 (모드=%s, 상태파일=%s). 설정: %s", self.mode, self.state_path,
                 {k: self.cfg[k] for k in ("lookback_days", "hold_days", "top_k", "exposure", "leverage",
                                           "catastrophe_stop", "capital_usdt")})
        last_log = ""
        while True:
            try:
                now = datetime.now(timezone.utc)
                prices = self._prices()
                self.check_stops(prices)
                if self.due(now):
                    self.rebalance()
                day = now.strftime("%Y-%m-%d %H")
                if day != last_log and now.minute < 10 and now.hour % 6 == 0:
                    eq = self.equity(prices)
                    self.state["history"] = (self.state["history"] + [[now.isoformat(), round(eq, 2)]])[-2000:]
                    self._save()
                    nxt = self.state.get("last_rebalance")
                    log.info("평가자산 %.2f USDT, 포지션 %d개, 마지막 리밸런스 %s", eq,
                             len(self.state["positions"]), (nxt or "")[:10])
                    last_log = day
            except BinanceError as e:
                log.error("거래소 오류: %s", e)
            except Exception:
                log.exception("예상치 못한 오류")
            time.sleep(60)
