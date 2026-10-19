"""코인 순환매(횡단면 모멘텀) 백테스트 - 지표 신호가 아니라 '최근 가장 강한 코인'을 사서 들고 가는 방식.

  python3 momentum_lab.py 2>&1 | tee momentum_result.txt
  python3 momentum_lab.py --days 1095 --top 40

규칙: 매주(또는 N일마다) 거래대금 상위 코인을 지난 L일 수익률로 줄 세워
  롱    : 상위 k개를 같은 금액으로 매수 (BTC 가 50일선 아래면 쉬기 = 하락장 회피)
  롱숏  : 상위 k개 매수 + 하위 k개 매도 (시장 방향 중립)
총 노출 = 자본 x1 (레버리지 1배 상당). 교체 시 수수료+슬리피지 0.07% 반영. 펀딩비는 미반영.
앞 70% / 뒤 30% 기간이 모두 플러스여야 통과.

편향 점검: '오늘 거래대금 상위'로 과거를 돌리면, 최근에 폭등해서 상위에 올라온 코인을 미리 알고 고르는 셈이 된다.
그래서 두 가지로 돌린다.
  [A] 오늘 기준 상위 코인 (편향 있음, 참고용)
  [B] 그 시점 기준 상위 코인 - 넓게(상위 100) 받아두고, 매 교체일마다 '그날까지 30일 평균 거래대금'
      상위 30개 중 상장 60일 이상만 대상. 롱 포지션은 펀딩비(하루 0.03%)도 비용으로 뺀다.  ← 이걸로 판단
"""
import argparse
import logging

import numpy as np
import pandas as pd

from bot.config import load_config
from bot.data import load_klines, top_symbols
from bot.exchange import BinanceFutures

COST = 0.0007
FUNDING_PER_DAY = 0.0003   # 롱이 내는 펀딩비 가정 (0.01%/8시간)


def run(closes: pd.DataFrame, btc_ok: pd.Series, lookback: int, hold: int, k: int, mode: str,
        eligible: pd.DataFrame | None = None, funding: float = 0.0):
    rets = closes.pct_change()
    mom = closes / closes.shift(lookback) - 1
    if eligible is not None:
        mom = mom.where(eligible)
    w = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    cur = pd.Series(0.0, index=closes.columns)
    for i, t in enumerate(closes.index):
        if i >= lookback and i % hold == 0:
            m = mom.loc[t].dropna()
            cur = pd.Series(0.0, index=closes.columns)
            if len(m) >= 2 * k:
                m = m.sort_values()
                if mode == "long":
                    if btc_ok.loc[t]:
                        cur[m.index[-k:]] = 1.0 / k
                else:
                    cur[m.index[-k:]] = 0.5 / k
                    cur[m.index[:k]] = -0.5 / k
        w.loc[t] = cur
    held = w.shift(1).fillna(0)                       # 오늘 종가에 정하고 다음 날 수익부터 반영
    gross = (held * rets.fillna(0)).sum(axis=1)
    turnover = w.diff().abs().sum(axis=1).fillna(w.abs().sum(axis=1))
    net = gross - turnover.shift(1).fillna(0) * COST - held.clip(lower=0).sum(axis=1) * funding
    return net


def stats(net: pd.Series):
    eq = (1 + net).cumprod()
    days = (net.index[-1] - net.index[0]).days or 1
    weekly = (1 + net).resample("W").prod() - 1
    cut = net.index[0] + (net.index[-1] - net.index[0]) * 0.7
    a, b = net[net.index < cut], net[net.index >= cut]
    return {
        "return%": round((eq.iloc[-1] - 1) * 100, 1),
        "연환산%": round((eq.iloc[-1] ** (365 / days) - 1) * 100, 1),
        "MDD%": round((eq / eq.cummax() - 1).min() * 100, 1),
        "주간승률%": round((weekly[weekly != 0] > 0).mean() * 100, 1),
        "앞70%": round(((1 + a).prod() - 1) * 100, 1),
        "뒤30%": round(((1 + b).prod() - 1) * 100, 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--top", type=int, default=35, help="[A] 오늘 기준 상위 N")
    ap.add_argument("--wide", type=int, default=100, help="[B] 받아둘 후보 수")
    ap.add_argument("--pit", type=int, default=30, help="[B] 그 시점 거래대금 상위 N")
    ap.add_argument("--min-qv", type=float, default=5e6, help="[B] 후보 최소 24h 거래대금 (USDT)")
    ap.add_argument("--min-age", type=int, default=60, help="[B] 상장 후 최소 일수")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = load_config()
    client = BinanceFutures()
    u = cfg["universe"]
    # 후보는 넓게: config 의 1억 USDT 기준을 쓰면 오늘 상위 35개만 남아 [B] 도 편향된다
    wide = top_symbols(client, args.wide, args.min_qv, u.get("exclude"))
    if "BTCUSDT" not in wide:
        wide.append("BTCUSDT")
    closes, qvol = {}, {}
    for s in wide:
        df = load_klines(client, s, "1d", args.days)
        if len(df) >= 60:
            closes[s], qvol[s] = df["close"], df["volume"] * df["close"]   # 거래대금(USDT) 근사
    closes, qvol = pd.DataFrame(closes).sort_index(), pd.DataFrame(qvol).sort_index()
    btc = closes["BTCUSDT"]
    btc_ok = (btc > btc.rolling(50).mean()).fillna(False)
    if closes.shape[1] < args.pit + 20:
        print(f"⚠ 후보가 {closes.shape[1]}개뿐이라 [B] 도 편향이 남습니다. --min-qv 를 낮춰 보세요.")
    print("후보 코인 수:", closes.shape[1], " 기간:", closes.index[0].date(), "~", closes.index[-1].date())

    today_top = [s for s in wide[:args.top] if s in closes.columns]
    age = closes.notna().cumsum()
    qv30 = qvol.rolling(30, min_periods=20).mean()
    pit = (qv30.rank(axis=1, ascending=False) <= args.pit) & (age >= args.min_age)

    scenarios = [
        ("A 오늘기준상위(편향)", closes[today_top], None, 0.0),
        ("B 시점기준상위+펀딩", closes, pit, FUNDING_PER_DAY),
    ]
    pd.set_option("display.width", 200)
    for name, cl, elig, fund in scenarios:
        rows = [{"전략": "BTC 그냥 보유", **stats(btc.pct_change().fillna(0))}]
        for mode in ("long", "longshort"):
            for lb in (7, 14, 28):
                for hold in (7, 14):
                    for k in (3, 5):
                        net = run(cl, btc_ok, lb, hold, k, mode, elig, fund)
                        rows.append({"전략": f"{'롱' if mode == 'long' else '롱숏'} L{lb} 보유{hold}일 상위{k}", **stats(net)})
        res = pd.DataFrame(rows)
        print(f"\n===== [{name}] 순환매 결과 (최근 {args.days}일, 수수료 포함) =====")
        print(res.to_string(index=False))
        ok = res[(res["앞70%"] > 0) & (res["뒤30%"] > 0) & (res["전략"] != "BTC 그냥 보유")]
        print(f"\n----- [{name}] 통과: 앞·뒤 기간 모두 플러스 ({len(ok)}/{len(res) - 1}) -----")
        print(ok.to_string(index=False) if len(ok) else "없음")

if __name__ == "__main__":
    main()
