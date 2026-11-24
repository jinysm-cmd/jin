"""세력 흔적 기반 일봉 신호 (마감봉 기준, 다음 날 시가 진입).

A  SHAKEOUT  털기 복귀: 조용한 박스권(매집) 하단을 잠깐 깼다가 며칠 안에 박스 안으로 복귀
B  PULLBACK  돌파 눌림: 거래대금 폭발 돌파 뒤, 거래량이 마른 채로 돌파 가격 근처까지 눌림
C  BREAKOUT  거래대금 폭발 돌파 당일 (비교용 기준 전략)

DIST 분산 흔적: 최근 60일 최대 거래량인데 윗꼬리가 길거나 음봉 → 보유 중이면 다음 날 매도
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Params:
    # 공통 유동성 필터
    min_tv20: float = 3e9          # 20일 평균 거래대금 (원)
    min_price: float = 1000.0
    min_bars: int = 250            # 상장 1년 미만 제외
    # A: 털기 복귀
    box_len: int = 60
    box_max_width: float = 0.40    # 박스 고점/저점 - 1
    accum_ratio: float = 1.1       # 박스 안 상승일 거래량 / 하락일 거래량
    shake_window: int = 5          # 하단 이탈 후 복귀까지 허용 일수
    shake_max_vol: float = 2.5     # 이탈일 거래량 / 평균 (진짜 투매면 제외)
    # B/C: 돌파
    brk_len: int = 20
    brk_tv_mult: float = 3.0       # 돌파일 거래대금 / 직전 20일 평균
    brk_min_ret: float = 0.05
    brk_min_clv: float = 0.6       # 종가가 봉의 위쪽 60% 이상
    pb_min_age: int = 2
    pb_max_age: int = 10
    pb_vol_ratio: float = 0.5      # 눌림일 거래량 / 돌파일 거래량
    pb_zone: float = 0.03          # 돌파 레벨 + 3% 이내까지 눌림
    # 손절 폭 한도
    min_stop: float = 0.03
    max_stop: float = 0.15
    # 분산
    dist_len: int = 60
    dist_wick: float = 0.5


def _clip_stop(close: pd.Series, raw: pd.Series, p: Params) -> pd.Series:
    lo = close * (1 - p.max_stop)
    hi = close * (1 - p.min_stop)
    return raw.clip(lower=lo, upper=hi)


def compute(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    o, h, l, c, v = (df[k] for k in ("open", "high", "low", "close", "volume"))
    out = pd.DataFrame(index=df.index)
    tv = c * v
    tv20 = tv.rolling(20).mean()
    vol20 = v.rolling(20).mean()
    rng = (h - l).replace(0, np.nan)
    clv = ((c - l) / rng).fillna(0.5)
    nbar = pd.Series(np.arange(len(df)), index=df.index)
    liquid = (tv20 >= p.min_tv20) & (c >= p.min_price) & (nbar >= p.min_bars)
    out["tv20"] = tv20

    # ---------- A: 털기 복귀 ----------
    w = p.shake_window
    box_low = l.shift(w + 1).rolling(p.box_len).min()
    box_high = h.shift(w + 1).rolling(p.box_len).max()
    up_vol = v.where(c > c.shift(1), 0.0).shift(w + 1).rolling(p.box_len).sum()
    dn_vol = v.where(c < c.shift(1), 0.0).shift(w + 1).rolling(p.box_len).sum()
    quiet_box = (box_high / box_low - 1 <= p.box_max_width) & (up_vol >= p.accum_ratio * dn_vol)
    broke = c < box_low * 0.99
    broke_soft = broke & (v <= p.shake_max_vol * vol20)
    any_break = broke.rolling(w).max().shift(1).fillna(0).astype(bool)
    soft_break = broke_soft.rolling(w).max().shift(1).fillna(0).astype(bool)
    # 이탈 중 거래량 폭발(진짜 투매)이 하나라도 있으면 제외
    hard_break = (broke & ~broke_soft).rolling(w).max().shift(1).fillna(0).astype(bool)
    reclaim = (c > box_low) & (c > o)
    out["sigA"] = liquid & quiet_box & any_break & soft_break & ~hard_break & reclaim & ~broke
    rawA = l.rolling(w + 1).min() * 0.99
    out["stopA"] = _clip_stop(c, rawA, p)
    out["scoreA"] = (box_high / c - 1)  # 박스 상단까지 남은 거리가 클수록 우선

    # ---------- C: 거래대금 폭발 돌파 ----------
    level = h.shift(1).rolling(p.brk_len).max()
    tv_prev = tv.shift(1).rolling(20).mean()
    brk = (
        (c > level) & (tv >= p.brk_tv_mult * tv_prev) & (c / c.shift(1) - 1 >= p.brk_min_ret)
        & (clv >= p.brk_min_clv) & (c / c.shift(1) - 1 < 0.29)  # 상한가 마감은 다음 날 못 산다고 보고 제외
    )
    out["sigC"] = liquid & brk
    out["stopC"] = _clip_stop(c, l, p)
    out["scoreC"] = tv / tv_prev

    # ---------- B: 돌파 눌림 ----------
    idx = nbar.where(brk).ffill()
    age = nbar - idx
    b_close = c.where(brk).ffill()
    b_low = l.where(brk).ffill()
    b_level = level.where(brk).ffill()
    b_vol = v.where(brk).ffill()
    pb = (
        (age >= p.pb_min_age) & (age <= p.pb_max_age)
        & (l <= b_level * (1 + p.pb_zone)) & (c >= b_level * 0.98) & (c > b_low)
        & (v <= p.pb_vol_ratio * b_vol)
    )
    out["sigB"] = liquid & pb
    out["stopB"] = _clip_stop(c, b_low * 0.99, p)
    out["scoreB"] = b_close / c - 1 + 1.0  # 돌파 종가보다 많이 눌렸을수록 우선

    # ---------- 분산 흔적 ----------
    upper_wick = (h - np.maximum(o, c)) / rng
    vmax = v.rolling(p.dist_len).max()
    ma20 = c.rolling(20).mean()
    out["dist"] = (v >= vmax) & ((upper_wick.fillna(0) >= p.dist_wick) | (c < o)) & (c > ma20)
    return out
