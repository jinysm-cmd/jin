"""실시간 자동매매 루프 (paper / testnet / live 공용)."""
import json
import logging
import os
import time
from datetime import datetime, timezone

import pandas as pd

from .data import top_symbols
from .exchange import INTERVAL_MS, MAINNET, BinanceError, BinanceFutures
from .risk import notional_fraction
from .strategy import StrategyParams, generate_signals, warmup_bars

log = logging.getLogger("trader")


class PaperBroker:
    """주문을 실제로 내지 않고 시세 기준으로 가상 체결."""

    def __init__(self, equity: float, fee: float = 0.0005, slippage: float = 0.0002):
        self.equity = equity
        self.fee, self.slip = fee, slippage

    def balance(self) -> float:
        return self.equity

    def open(self, symbol, side, qty, price):
        fill = price * (1 + self.slip * side)
        self.equity -= abs(qty) * fill * self.fee
        return fill

    def close(self, symbol, side, qty, price, entry, stop_ref=None):
        fill = price * (1 - self.slip * side)
        self.equity -= abs(qty) * fill * self.fee
        self.equity += side * qty * (fill - entry)
        return fill

    def exchange_positions(self):
        return None


class LiveBroker:
    def __init__(self, client: BinanceFutures, leverage: int, exchange_stop: bool):
        self.c = client
        self.leverage = leverage
        self.exchange_stop = exchange_stop
        self._prepared: set[str] = set()
        self._preflight()

    def _preflight(self):
        """봇은 단방향(One-way) 포지션 모드 기준으로 주문한다. 헤지 모드면 전환을 시도."""
        if self.c.is_hedge_mode():
            try:
                self.c.set_one_way_mode()
                log.info("포지션 모드를 헤지 → 단방향(One-way)으로 전환했습니다.")
            except BinanceError as e:
                raise SystemExit(
                    f"계정이 헤지 모드인데 단방향 전환에 실패했습니다({e}). 열린 포지션/주문을 모두 정리한 뒤 "
                    "바이낸스 선물 화면 → 설정(톱니바퀴) → 포지션 모드 → 단방향(One-way)으로 바꾸고 다시 실행하세요.")

    def balance(self) -> float:
        return self.c.balance_usdt()

    def _prepare(self, symbol):
        if symbol in self._prepared:
            return
        try:
            self.c.set_isolated(symbol)
        except BinanceError as e:  # 멀티에셋 모드 등에서는 격리 마진 불가 → 교차 마진으로 진행
            log.warning("%s 격리 마진 설정 실패, 현재 마진 방식으로 진행: %s", symbol, e)
        self.c.set_leverage(symbol, self.leverage)
        self._prepared.add(symbol)

    def open(self, symbol, side, qty, price):
        self._prepare(symbol)
        self.c.cancel_all(symbol)  # 이전 거래에서 남은 손절 주문이 새 포지션을 건드리지 않도록 정리
        r = self.c.market_order(symbol, "BUY" if side == 1 else "SELL", qty)
        avg = float(r.get("avgPrice") or 0) or price
        return avg

    def protect(self, symbol, side, stop_price):
        if not self.exchange_stop:
            return
        try:
            ref = self.c.place_protective_stop(symbol, "SELL" if side == 1 else "BUY", stop_price)
            log.info("%s 거래소 비상손절 등록 완료 @%.6g (%s)", symbol, stop_price, ref)
            return ref
        except BinanceError as e:
            log.warning("%s 거래소 비상손절 등록 실패 (봇 감시 손절은 동작): %s", symbol, e)
            return None

    def close(self, symbol, side, qty, price, entry, stop_ref=None):
        r = self.c.market_order(symbol, "SELL" if side == 1 else "BUY", qty, reduce_only=True)
        self.c.cancel_protective_stop(symbol, stop_ref)
        self.c.cancel_all(symbol)
        return float(r.get("avgPrice") or 0) or price

    def exchange_positions(self):
        return self.c.positions()


class Trader:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.mode = cfg["mode"]
        self.interval = cfg["interval"]
        self.bar_ms = INTERVAL_MS[self.interval]
        self.params = StrategyParams.from_dict(cfg.get("strategy"))
        self.state_path = f"state_{self.mode}.json"

        if self.mode == "paper":
            self.client = BinanceFutures(base_url=MAINNET)  # 시세만 사용
        else:
            key = os.environ.get(cfg["api_key_env"], "")
            sec = os.environ.get(cfg["api_secret_env"], "")
            if not key or not sec:
                raise SystemExit(f"환경변수 {cfg['api_key_env']} / {cfg['api_secret_env']} 를 설정하세요")
            base = cfg.get("testnet_url") if self.mode == "testnet" else MAINNET
            self.client = BinanceFutures(key, sec, base_url=base)
        self.client.sync_time()
        self.client.load_filters()

        self.state = self._load_state()
        if self.mode == "paper":
            costs = cfg.get("costs", {})
            self.broker = PaperBroker(self.state.get("paper_equity", cfg.get("paper_start_equity", 1000)),
                                      costs.get("fee", 0.0005), costs.get("slippage", 0.0002))
        else:
            self.broker = LiveBroker(self.client, int(cfg["leverage"]), cfg.get("exchange_stop", True))
        self.symbols: list[str] = []
        self.symbols_at = 0.0
        self.last_bar_processed = self.state.get("last_bar_processed", 0)

    # ------------------------------------------------------------ 상태 저장
    def _load_state(self) -> dict:
        if os.path.exists(self.state_path):
            with open(self.state_path) as fh:
                return json.load(fh)
        return {"positions": {}, "cooldown": {}, "day": "", "day_start_equity": None, "trades": []}

    def _save(self):
        self.state["last_bar_processed"] = self.last_bar_processed
        if isinstance(self.broker, PaperBroker):
            self.state["paper_equity"] = self.broker.equity
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.state, fh, indent=2, default=str)
        os.replace(tmp, self.state_path)

    # ------------------------------------------------------------ 유틸
    def _refresh_universe(self):
        hours = self.cfg.get("universe", {}).get("refresh_hours", 6)
        if self.symbols and time.time() - self.symbols_at < hours * 3600:
            return
        if self.cfg["symbols"] == "auto":
            u = self.cfg["universe"]
            self.symbols = top_symbols(self.client, u["top_n"], u["min_quote_volume"], u.get("exclude"))
        else:
            self.symbols = list(self.cfg["symbols"])
        self.symbols_at = time.time()
        log.info("거래 대상 %d개: %s", len(self.symbols), ", ".join(self.symbols))

    def _marks(self) -> dict[str, dict]:
        return {d["symbol"]: d for d in self.client.premium_index()}

    def _daily_guard(self, equity: float) -> bool:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.state["day"] != today:
            self.state["day"] = today
            self.state["day_start_equity"] = equity
        start = self.state["day_start_equity"] or equity
        dd = equity / start - 1
        if dd <= -self.cfg["max_daily_loss"]:
            log.warning("일일 손실 한도 도달 (%.2f%%). 오늘은 신규 진입 중단.", dd * 100)
            return False
        target = self.cfg.get("daily_profit_stop", 0) or 0
        if target > 0 and dd >= target:
            log.info("일일 목표 수익 달성 (+%.2f%%). 오늘은 신규 진입 중단.", dd * 100)
            return False
        return True

    # ------------------------------------------------------------ 포지션 관리
    def manage_positions(self, marks: dict):
        now_ms = int(time.time() * 1000)
        ex_pos = self.broker.exchange_positions()
        self._ex_pos = ex_pos or {}
        for sym, pos in list(self.state["positions"].items()):
            if ex_pos is not None and sym not in ex_pos:
                log.info("%s 포지션이 거래소에서 이미 청산됨(비상손절/수동). 상태에서 제거.", sym)
                self._record(sym, pos, pos["sl"], "EXCHANGE")
                continue
            m = marks.get(sym)
            if not m:
                continue
            px = float(m["markPrice"])
            side = pos["side"]
            reason = None
            if (side == 1 and px <= pos["sl"]) or (side == -1 and px >= pos["sl"]):
                reason = "SL"
            elif (side == 1 and px >= pos["tp"]) or (side == -1 and px <= pos["tp"]):
                reason = "TP"
            elif now_ms >= pos["expire_ms"]:
                reason = "TIME"
            be_r = self.params.breakeven_r
            if not reason and be_r > 0 and not pos.get("be"):
                risk = abs(pos["entry"] - pos.get("sl0", pos["sl"]))
                if side * (px - pos["entry"]) >= be_r * risk:
                    pos["sl"] = pos["entry"] * (1 + side * 0.0014)
                    pos["be"] = True
                    self._save()
                    log.info("%s 수익 %.1fR 도달 → 손절을 본전(%.6g)으로 이동", sym, be_r, pos["sl"])
            if reason:
                try:
                    fill = self.broker.close(sym, side, pos["qty"], px, pos["entry"], pos.get("stop_ref"))
                except BinanceError as e:
                    log.error("%s 청산 실패: %s", sym, e)
                    continue
                self._record(sym, pos, fill, reason)

    def _record(self, sym, pos, exit_px, reason):
        pnl = pos["side"] * pos["qty"] * (exit_px - pos["entry"])
        log.info("청산 %s %s %s @%.6g → %s  PnL(수수료 전)=%.2f USDT",
                 pos["setup"], "LONG" if pos["side"] == 1 else "SHORT", sym, exit_px, reason, pnl)
        self.state["trades"].append({**pos, "symbol": sym, "exit": exit_px, "reason": reason, "pnl": pnl,
                                     "exit_time": datetime.now(timezone.utc).isoformat()})
        self.state["trades"] = self.state["trades"][-500:]
        del self.state["positions"][sym]
        self.state["cooldown"][sym] = int(time.time() * 1000) + self.cfg.get("cooldown_bars", 3) * self.bar_ms
        self._save()

    # ------------------------------------------------------------ 신호/진입
    def scan(self, marks: dict):
        equity = self.broker.balance()
        if not self._daily_guard(equity):
            return
        n_need = warmup_bars(self.params) + 60
        now = pd.Timestamp.now(tz="UTC")
        for sym in self.symbols:
            if len(self.state["positions"]) >= self.cfg["max_positions"]:
                break
            if sym in self.state["positions"] or self.state["cooldown"].get(sym, 0) > now.value // 1_000_000:
                continue
            if sym in getattr(self, "_ex_pos", {}):  # 직접(수동) 잡은 포지션이 있는 코인은 건드리지 않음
                continue
            try:
                df = self.client.klines(sym, self.interval, limit=min(1500, n_need))
            except BinanceError as e:
                log.warning("%s 캔들 조회 실패: %s", sym, e)
                continue
            df = df[df["close_time"] < now]  # 마감된 봉만
            if len(df) < warmup_bars(self.params) + 2:
                continue
            fr = float(marks.get(sym, {}).get("lastFundingRate", 0) or 0)
            f = generate_signals(df, self.params, pd.Series(fr, index=df.index))
            last = f.iloc[-1]
            if last["signal"] == 0:
                continue
            self._enter(sym, int(last["signal"]), last, marks, equity)

    def _enter(self, sym, side, row, marks, equity):
        px = float(marks[sym]["markPrice"]) if sym in marks else float(row["close"])
        frac = notional_fraction(px, row["sl_dist"], self.cfg["risk_per_trade"], self.cfg["leverage"],
                                 self.cfg["max_positions"], self.cfg.get("margin_per_trade", 0) or 0)
        qty = self.client.round_qty(sym, frac * equity / px)
        f = self.client.filters(sym)
        if qty < f["min_qty"] or qty * px < f["min_notional"]:
            log.info("%s 신호(%s)지만 최소 주문 수량 미달로 스킵 (계좌가 작음)", sym, row["setup"])
            return
        try:
            fill = self.broker.open(sym, side, qty, px)
        except BinanceError as e:
            log.error("%s 진입 실패: %s", sym, e)
            return
        sl = fill - side * row["sl_dist"]
        tp = fill + side * row["tp_dist"]
        pos = {
            "side": side, "qty": qty, "entry": fill, "sl": sl, "sl0": sl, "tp": tp, "setup": row["setup"],
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "expire_ms": int(time.time() * 1000) + self.params.max_hold_bars * self.bar_ms,
        }
        self.state["positions"][sym] = pos
        self._save()
        if isinstance(self.broker, LiveBroker):
            pos["stop_ref"] = self.broker.protect(sym, side, sl)
            self._save()
        log.info("진입 %s %s %s qty=%s @%.6g  SL=%.6g TP=%.6g", row["setup"], "LONG" if side == 1 else "SHORT",
                 sym, qty, fill, sl, tp)

    # ------------------------------------------------------------ 메인 루프
    def maybe_scan(self, marks: dict, now_ms: int) -> bool:
        """새 봉이 막 마감됐을 때만 신호를 확인한다.

        봉 마감 후 한참 지나서(봇을 중간에 켰거나 재시작) 지난 신호로 진입하면 백테스트와 다른
        가격에 들어가게 되므로, 마감 직후 일정 시간 안에만 스캔하고 그 외엔 다음 봉을 기다린다.
        """
        bar_open = now_ms // self.bar_ms * self.bar_ms  # 현재 진행 중인 봉 시작 = 직전 봉 마감
        if bar_open <= self.last_bar_processed or now_ms - bar_open < 3000:  # 마감 3초 후 처리
            return False
        window = max(60_000, min(self.bar_ms // 4, 15 * 60_000))
        scanned = now_ms - bar_open <= window
        if scanned:
            self.scan(marks)
        else:
            nxt = datetime.fromtimestamp((bar_open + self.bar_ms) / 1000).strftime("%H:%M")
            log.info("직전 봉 마감 후 시간이 지나 이번 신호는 건너뜀. 다음 봉 마감(%s)부터 확인합니다.", nxt)
        self.last_bar_processed = bar_open
        self._save()
        log.info("자산 %.2f USDT | 보유 %d개 %s", self.broker.balance(), len(self.state["positions"]),
                 list(self.state["positions"]))
        return scanned

    def run(self):
        log.info("모드=%s 인터벌=%s 시작. 상태파일=%s", self.mode, self.interval, self.state_path)
        if self.mode == "live":
            log.warning("!!! 실계좌 모드입니다. 손실 위험이 있습니다 !!!")
        if self.mode != "paper":
            log.info("연결 확인: USDT 잔고 %.2f, 거래소 보유 포지션 %s",
                     self.broker.balance(), list((self.broker.exchange_positions() or {}).keys()) or "없음")
        while True:
            try:
                self._refresh_universe()
                marks = self._marks()
                self.manage_positions(marks)
                self.maybe_scan(marks, int(time.time() * 1000))
            except BinanceError as e:
                log.error("API 오류: %s", e)
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("예상치 못한 오류")
            time.sleep(self.cfg.get("poll_seconds", 10))
