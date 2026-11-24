"""일봉 포트폴리오 백테스트 (현물 롱 전용).

하루 순서
  1) 시가: 어제 종가에 정한 매도(분산/추적손절/보유기간) 체결, 시가가 손절가 아래면 시가에 손절
  2) 시가: 어제 종가 신호로 신규 매수 (빈 자리만, 점수 높은 순)
  3) 장중: 저가가 손절가에 닿으면 손절가에 체결 (진입 당일 포함, 보수적)
  4) 종가: 평가금액 갱신, 내일 매도 여부 결정
비용: 매수 수수료+슬리피지, 매도 수수료+슬리피지+증권거래세(연도별)
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

FEE = 0.00015
SLIP = 0.001


def sell_tax(date: pd.Timestamp) -> float:
    """증권거래세+농특세 근사치 (코스피/코스닥 동일하게 취급)."""
    y = date.year
    if date < pd.Timestamp("2019-06-03"):
        return 0.0030
    if y <= 2020:
        return 0.0025
    if y <= 2022:
        return 0.0023
    if y == 2023:
        return 0.0020
    if y == 2024:
        return 0.0018
    if y == 2025:
        return 0.0015
    return 0.0020


@dataclass
class EngineCfg:
    setups: tuple = ("A", "B")
    max_pos: int = 10
    max_hold: int = 20
    trail: float = 0.12           # 보유 중 최고 종가 대비 하락폭
    use_dist_exit: bool = True
    max_gap_up: float = 0.08      # 다음 날 시가가 신호 종가보다 8% 넘게 뜨면 추격 안 함
    start: str = "2016-01-01"
    end: str = "2100-01-01"
    init_cash: float = 100_000_000


@dataclass
class Pos:
    code: str
    setup: str
    entry_date: pd.Timestamp
    entry_px: float
    shares: float
    stop: float
    hi_close: float
    bars: int = 0
    exit_next: str = ""


@dataclass
class Result:
    equity: pd.Series
    trades: pd.DataFrame
    stats: dict = field(default_factory=dict)


def prepare(data: dict, sigs: dict) -> dict:
    """종목별 numpy 배열 + 날짜→행 번호."""
    prep = {}
    for code, df in data.items():
        s = sigs[code]
        prep[code] = {
            "pos": {d: i for i, d in enumerate(df.index)},
            "o": df["open"].to_numpy(), "h": df["high"].to_numpy(),
            "l": df["low"].to_numpy(), "c": df["close"].to_numpy(),
            "dist": s["dist"].to_numpy(),
            "dates": df.index,
        }
    return prep


def signal_book(sigs: dict, setups) -> dict:
    """날짜 → [(score, code, setup, stop, close)]."""
    book = {}
    for code, s in sigs.items():
        for st in setups:
            m = s[f"sig{st}"].to_numpy(dtype=bool)
            if not m.any():
                continue
            sub = s.loc[m, [f"score{st}", f"stop{st}"]]
            for d, sc, sp in zip(sub.index, sub.iloc[:, 0], sub.iloc[:, 1]):
                book.setdefault(d, []).append((float(sc), code, st, float(sp)))
    return book


def run(prep: dict, book: dict, cfg: EngineCfg) -> Result:
    cal = sorted({d for p in prep.values() for d in p["dates"]})
    cal = [d for d in cal if pd.Timestamp(cfg.start) <= d <= pd.Timestamp(cfg.end)]
    cash = cfg.init_cash
    held: dict[str, Pos] = {}
    trades, eq = [], []
    last_close: dict[str, float] = {}
    prev_day = None

    def close_pos(p: Pos, px: float, d, why: str):
        nonlocal cash
        px_net = px * (1 - SLIP - FEE - sell_tax(d))
        cash += p.shares * px_net
        trades.append({
            "code": p.code, "setup": p.setup, "entry_date": p.entry_date, "exit_date": d,
            "entry": p.entry_px, "exit": px, "ret": px_net / p.entry_px - 1, "bars": p.bars, "why": why,
        })
        del held[p.code]

    for d in cal:
        # 1) 시가 매도
        for p in list(held.values()):
            a = prep[p.code]
            i = a["pos"].get(d)
            if i is None:
                continue  # 거래정지 등
            o = a["o"][i]
            if p.exit_next:
                close_pos(p, o, d, p.exit_next)
            elif o <= p.stop:
                close_pos(p, o, d, "stop_gap")

        # 2) 시가 매수
        if prev_day is not None and len(held) < cfg.max_pos:
            cands = sorted(book.get(prev_day, []), reverse=True)
            equity_now = cash + sum(p.shares * last_close.get(p.code, p.entry_px) for p in held.values())
            for sc, code, st, stop in cands:
                if len(held) >= cfg.max_pos:
                    break
                if code in held:
                    continue
                a = prep[code]
                i = a["pos"].get(d)
                if i is None or i == 0:
                    continue
                o, sig_close = a["o"][i], a["c"][i - 1]
                if o > sig_close * (1 + cfg.max_gap_up) or o <= stop:
                    continue
                budget = min(equity_now / cfg.max_pos, cash)
                if budget < equity_now / cfg.max_pos * 0.5:
                    break
                px = o * (1 + SLIP + FEE)
                shares = budget / px
                cash -= shares * px
                held[code] = Pos(code, st, d, px, shares, stop, o)

        # 3) 장중 손절, 4) 종가 처리
        for p in list(held.values()):
            a = prep[p.code]
            i = a["pos"].get(d)
            if i is None:
                continue
            if a["l"][i] <= p.stop:
                close_pos(p, min(p.stop, a["o"][i]), d, "stop")
                continue
            c = a["c"][i]
            last_close[p.code] = c
            p.bars += 1
            p.hi_close = max(p.hi_close, c)
            if cfg.use_dist_exit and a["dist"][i]:
                p.exit_next = "dist"
            elif c <= p.hi_close * (1 - cfg.trail):
                p.exit_next = "trail"
            elif p.bars >= cfg.max_hold:
                p.exit_next = "time"
        eq.append((d, cash + sum(p.shares * last_close.get(p.code, p.entry_px) for p in held.values())))
        prev_day = d

    equity = pd.Series(dict(eq))
    tr = pd.DataFrame(trades)
    return Result(equity, tr, stats(equity, tr))


def stats(equity: pd.Series, tr: pd.DataFrame) -> dict:
    if equity.empty:
        return {}
    yrs = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    total = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (1 + total) ** (1 / yrs) - 1
    dd = (equity / equity.cummax() - 1).min()
    r = equity.pct_change().dropna()
    sharpe = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else 0.0
    out = {"total": total, "cagr": cagr, "mdd": dd, "sharpe": sharpe, "trades": len(tr)}
    if len(tr):
        win = tr["ret"] > 0
        gains, losses = tr.loc[win, "ret"].sum(), -tr.loc[~win, "ret"].sum()
        out.update({
            "winrate": win.mean(), "avg_ret": tr["ret"].mean(),
            "pf": gains / losses if losses > 0 else np.inf,
            "avg_bars": tr["bars"].mean(),
        })
    return out
