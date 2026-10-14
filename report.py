"""실거래 성과 리포트.

  python3 report.py 2>&1 | tee report.txt          (실거래 폴더에서, export 로 API 키 입력 후)
  python3 report.py --offline                         (API 없이 state_live.json 만으로)

1) 바이낸스 실제 손익: 실현손익 + 수수료 + 펀딩비 (거래소 기록 기준, 가장 정확)
2) 봇 거래 기록: 승률, 거래당 R, 셋업/청산사유/롱숏별
3) 현재 보유 포지션과 손절·익절가
4) 백테스트 기대치와 비교
"""
import argparse
import json
import os
import time
from datetime import datetime, timezone

import pandas as pd

from bot.config import load_config
from bot.exchange import BinanceFutures

EXPECT = {"승률%": "약 40%", "거래당 R": "약 +0.06 ~ +0.13", "하루 거래": "약 1~2회"}


def risk_of(t: dict) -> float:
    sl = t.get("sl0", t.get("sl"))
    try:
        return abs(float(t["entry"]) - float(sl)) * float(t["qty"])
    except (TypeError, ValueError, KeyError):
        return float("nan")


def bot_trades(state: dict) -> pd.DataFrame:
    rows = []
    for t in state.get("trades", []):
        r = risk_of(t)
        rows.append({
            "청산시각": t.get("exit_time", "")[:16].replace("T", " "),
            "코인": t.get("symbol"), "전략": t.get("setup"),
            "방향": "롱" if t.get("side") == 1 else "숏",
            "진입가": t.get("entry"), "청산가": t.get("exit"), "사유": t.get("reason"),
            "손익USDT": round(float(t.get("pnl", 0)), 2),
            "R": round(float(t.get("pnl", 0)) / r, 2) if r and r == r else None,
        })
    return pd.DataFrame(rows)


def section(title):
    print(f"\n{'=' * 10} {title} {'=' * 10}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config_4h.yaml")
    ap.add_argument("--state", default="state_live.json")
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()
    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 300)

    state = json.load(open(args.state, encoding="utf-8")) if os.path.exists(args.state) else {}
    tdf = bot_trades(state)
    first = None
    if not tdf.empty:
        first = min(t.get("entry_time", "9999") for t in state["trades"])

    client = None
    if not args.offline:
        cfg = load_config(args.config)
        key, sec = os.environ.get(cfg["api_key_env"], ""), os.environ.get(cfg["api_secret_env"], "")
        if key and sec:
            client = BinanceFutures(key, sec)
            client.sync_time()
        else:
            print("※ API 키 환경변수가 없어 거래소 조회는 건너뜁니다 (export 후 다시 실행하면 포함).")

    if client:
        section("1. 바이낸스 실제 손익 (거래소 기록)")
        now = int(time.time() * 1000)
        start = int(datetime.fromisoformat(first).timestamp() * 1000) - 3600_000 if first else now - 30 * 86_400_000
        inc = pd.DataFrame(client.income(start, now))
        bal = client.balance_usdt()
        if inc.empty:
            print("  기록 없음")
        else:
            inc["income"] = inc["income"].astype(float)
            inc["date"] = pd.to_datetime(inc["time"].astype("int64"), unit="ms", utc=True).dt.tz_convert("Asia/Seoul").dt.date
            kinds = {"REALIZED_PNL": "실현손익", "COMMISSION": "수수료", "FUNDING_FEE": "펀딩비",
                     "TRANSFER": "입출금(이체)"}
            by_kind = inc.groupby("incomeType")["income"].sum()
            for k, name in kinds.items():
                if k in by_kind:
                    print(f"  {name:>8}: {by_kind[k]:+.2f} USDT")
            trading = by_kind.drop(labels=[k for k in ("TRANSFER",) if k in by_kind]).sum()
            print(f"  {'매매 순손익':>8}: {trading:+.2f} USDT  (실현손익+수수료+펀딩비)")
            print(f"  {'현재 잔고':>8}: {bal:.2f} USDT")
            daily = inc[inc.incomeType != "TRANSFER"].groupby("date")["income"].sum().round(2)
            print("\n  [일별 순손익]")
            print(daily.to_string())
            sym = inc[inc.incomeType.isin(["REALIZED_PNL", "COMMISSION", "FUNDING_FEE"])].groupby("symbol")["income"].sum()
            print("\n  [코인별 순손익]")
            print(sym.sort_values().round(2).to_string())

    section("2. 봇 거래 기록")
    if tdf.empty:
        print("  아직 청산된 거래가 없습니다.")
    else:
        n = len(tdf)
        wins = (tdf["손익USDT"] > 0).sum()
        print(f"  청산 거래 {n}회 | 승 {wins} / 패 {n - wins} | 승률 {wins / n * 100:.1f}%")
        print(f"  손익 합계(수수료 전) {tdf['손익USDT'].sum():+.2f} USDT | 거래당 평균 {tdf['R'].mean():+.2f}R")
        print(f"  평균 이익 {tdf.loc[tdf['손익USDT'] > 0, '손익USDT'].mean():+.2f} / "
              f"평균 손실 {tdf.loc[tdf['손익USDT'] <= 0, '손익USDT'].mean():+.2f} USDT")
        for col in ("전략", "사유", "방향"):
            g = tdf.groupby(col).agg(거래=("R", "size"), 승률=("손익USDT", lambda x: f"{(x > 0).mean() * 100:.0f}%"),
                                    손익=("손익USDT", "sum"), 평균R=("R", "mean")).round(2)
            print(f"\n  [{col}별]")
            print(g.to_string())
        print("\n  [전체 거래 목록]")
        print(tdf.to_string(index=False))

    section("3. 현재 보유 포지션")
    pos = state.get("positions", {})
    if not pos:
        print("  없음")
    else:
        marks = {}
        if not args.offline:
            try:
                marks = {d["symbol"]: float(d["markPrice"]) for d in (client or BinanceFutures()).premium_index()}
            except Exception:
                pass
        rows = []
        for s, p in pos.items():
            side, m = p["side"], marks.get(s)
            rows.append({"코인": s, "전략": p["setup"], "방향": "롱" if side == 1 else "숏", "진입가": p["entry"],
                         "현재가": m, "손절가": round(p["sl"], 6), "익절가": round(p["tp"], 6),
                         "평가손익": round(side * p["qty"] * (m - p["entry"]), 2) if m else None,
                         "진입": p.get("entry_time", "")[:16].replace("T", " ")})
        print(pd.DataFrame(rows).to_string(index=False))

    section("4. 백테스트 기대치 (비교용)")
    for k, v in EXPECT.items():
        print(f"  {k}: {v}")
    print("  ※ 거래 30회 미만이면 우연의 영향이 커서 기대치와 달라도 판단하기 이릅니다.")


if __name__ == "__main__":
    main()
