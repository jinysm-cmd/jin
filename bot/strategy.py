"""오더플로우 3종 셋업 전략 (OrderFlow Trinity).

RSI/MACD/볼린저 크로스 같은 흔한 조건 대신, kline 의 테이커 체결 데이터로
'누가 공격적으로 사고팔았는데 가격은 어떻게 반응했는가'를 본다.

1) ABSORB  - 흡수 반전 (평균회귀)
   가격이 rolling VWAP 에서 크게 이탈한 상태에서, 거래량 폭발 + 시장가 매도가
   압도적인데도 캔들이 위쪽에서 마감 → 누군가 지정가로 매도 물량을 다 받아냄.
   매도 소진으로 보고 롱. (숏은 대칭)

2) TRAP    - 갇힌 돌파 매매자 역이용
   직전 봉이 N봉 고점을 돌파하면서 시장가 매수가 몰렸는데, 이번 봉에서 다시
   돌파 레벨 아래로 마감 + 매도 우위 → 추격 매수자들이 물림. 손절 물량을 노리고 숏.

3) SQUEEZE - 델타 확인 변동성 압축 돌파 (추세)
   변동성이 최근 대비 하위 구간으로 눌려있다가, 테이커 매수 우위 + 거래량 증가를
   동반해 고점 돌파 → 진짜 돌파로 보고 롱.

모든 신호는 '마감된 봉' 기준으로 계산하고 다음 봉 시가에 진입한다.
라이브 봇과 백테스터가 이 모듈을 그대로 공유한다.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import classic
from . import indicators as ind


@dataclass
class StrategyParams:
    # 공통
    atr_len: int = 14
    vol_z_len: int = 96
    max_hold_bars: int = 36
    # ABSORB
    absorb_enabled: bool = True
    vwap_len: int = 48
    dev_k: float = 2.0
    absorb_vol_z: float = 1.8
    absorb_imb: float = 0.12
    absorb_clv: float = 0.55
    absorb_sl_atr: float = 1.3
    absorb_tp_atr: float = 2.0
    # TRAP
    trap_enabled: bool = True
    trap_lookback: int = 24
    trap_imb: float = 0.15
    trap_vol_z: float = 1.0
    trap_sl_buffer_atr: float = 0.25
    trap_tp_r: float = 1.6
    # SQUEEZE
    squeeze_enabled: bool = True
    # breakout: 돌파 방향 추종 / fade: 돌파 실패에 역베팅 (진단 결과 fade 쪽이 전 타임프레임에서 수수료 전 플러스)
    squeeze_mode: str = "breakout"
    squeeze_bb_len: int = 20
    squeeze_rank_len: int = 120
    squeeze_rank_max: float = 0.20
    squeeze_breakout_len: int = 24
    squeeze_imb: float = 0.15
    squeeze_vol_z: float = 1.2
    squeeze_sl_atr: float = 1.2
    squeeze_tp_atr: float = 3.0
    # 펀딩비 필터: 같은 방향 쏠림이 심하면 진입 금지
    funding_block: float = 0.0005
    # 수수료 대비 손절폭이 너무 좁으면 수수료가 R 을 갉아먹음 → 최소 손절폭(가격 대비 %)과 최소 손익비
    min_sl_pct: float = 0.004
    min_rr: float = 1.3
    # 대표 지표 필터: EMA 추세 방향으로만 진입 (0 이면 끔). 예) 200 → 종가>EMA200 롱만, 종가<EMA200 숏만
    trend_ema_len: int = 0
    # 진입 엔진: orderflow(위 3종 셋업) 또는 classic.ENGINES 중 하나 (pullback/macd/bollinger/rsi2/heikin)
    engine: str = "orderflow"

    @classmethod
    def from_dict(cls, d: dict | None) -> "StrategyParams":
        d = d or {}
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def warmup_bars(p: StrategyParams) -> int:
    return max(p.vol_z_len, p.squeeze_rank_len + p.squeeze_bb_len, p.vwap_len * 2, p.trend_ema_len,
               210 if p.engine != "orderflow" else 0) + 5


def compute_features(df: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    out = df.copy()
    out["atr"] = ind.atr(out, p.atr_len)
    out["imb"] = ind.taker_imbalance(out)
    out["clv"] = ind.close_location(out)
    out["vol_z"] = ind.log_volume_z(out, p.vol_z_len)

    out["vwap"] = ind.rolling_vwap(out, p.vwap_len)
    dev = out["close"] - out["vwap"]
    out["dev_z"] = dev / dev.rolling(p.vwap_len).std().replace(0, np.nan)

    out["hh_trap"] = out["high"].rolling(p.trap_lookback).max().shift(1)
    out["ll_trap"] = out["low"].rolling(p.trap_lookback).min().shift(1)

    mid = out["close"].rolling(p.squeeze_bb_len).mean()
    bbw = out["close"].rolling(p.squeeze_bb_len).std() / mid
    out["bbw_rank"] = ind.rolling_pct_rank(bbw, p.squeeze_rank_len)
    out["hh_brk"] = out["high"].rolling(p.squeeze_breakout_len).max().shift(1)
    out["ll_brk"] = out["low"].rolling(p.squeeze_breakout_len).min().shift(1)
    return out


def generate_signals(df: pd.DataFrame, p: StrategyParams, funding: pd.Series | None = None) -> pd.DataFrame:
    """각 마감봉에 대해 signal(+1 롱/-1 숏/0), setup, sl_dist, tp_dist 를 붙여 반환.

    sl_dist/tp_dist 는 '진입가 기준 가격 거리'. 실제 진입가(다음 봉 시가)에서 더하고 뺀다.
    funding: df 인덱스에 맞춰 정렬된 펀딩비 시리즈(없으면 필터 미적용).
    """
    f = compute_features(df, p)
    a = f["atr"]
    sig = pd.Series(0, index=f.index, dtype=int)
    setup = pd.Series("", index=f.index, dtype=object)
    sl = pd.Series(np.nan, index=f.index)
    tp = pd.Series(np.nan, index=f.index)

    def put(mask, direction, name, sl_d, tp_d):
        # 우선순위: 먼저 들어간 셋업이 이긴다 (이미 신호가 있는 봉은 덮어쓰지 않음)
        m = mask.fillna(False) & (sig == 0)
        sig[m] = direction
        setup[m] = name
        sl[m] = sl_d[m] if isinstance(sl_d, pd.Series) else sl_d
        tp[m] = tp_d[m] if isinstance(tp_d, pd.Series) else tp_d

    if p.engine != "orderflow":
        classic.add_signals(f, p.engine, put)

    of = p.engine == "orderflow"
    # 1) TRAP: 돌파 실패는 빠르게 움직이므로 최우선
    if of and p.trap_enabled:
        prev = f.shift(1)
        broke_up = (prev["high"] > prev["hh_trap"]) & (prev["imb"] > p.trap_imb) & (prev["vol_z"] > p.trap_vol_z)
        trap_short = broke_up & (f["close"] < prev["hh_trap"]) & (f["imb"] < 0)
        sl_s = (prev["high"] - f["close"]).clip(lower=0) + p.trap_sl_buffer_atr * a
        sl_s = sl_s.clip(lower=0.5 * a)
        put(trap_short, -1, "TRAP", sl_s, sl_s * p.trap_tp_r)

        broke_dn = (prev["low"] < prev["ll_trap"]) & (prev["imb"] < -p.trap_imb) & (prev["vol_z"] > p.trap_vol_z)
        trap_long = broke_dn & (f["close"] > prev["ll_trap"]) & (f["imb"] > 0)
        sl_l = (f["close"] - prev["low"]).clip(lower=0) + p.trap_sl_buffer_atr * a
        sl_l = sl_l.clip(lower=0.5 * a)
        put(trap_long, 1, "TRAP", sl_l, sl_l * p.trap_tp_r)

    # 2) ABSORB
    if of and p.absorb_enabled:
        hot = f["vol_z"] > p.absorb_vol_z
        absorb_long = (f["dev_z"] < -p.dev_k) & hot & (f["imb"] < -p.absorb_imb) & (f["clv"] > p.absorb_clv)
        absorb_short = (f["dev_z"] > p.dev_k) & hot & (f["imb"] > p.absorb_imb) & (f["clv"] < 1 - p.absorb_clv)
        put(absorb_long, 1, "ABSORB", p.absorb_sl_atr * a, p.absorb_tp_atr * a)
        put(absorb_short, -1, "ABSORB", p.absorb_sl_atr * a, p.absorb_tp_atr * a)

    # 3) SQUEEZE
    if of and p.squeeze_enabled:
        squeezed = f["bbw_rank"].shift(1) < p.squeeze_rank_max
        vol_ok = f["vol_z"] > p.squeeze_vol_z
        sq_long = squeezed & vol_ok & (f["close"] > f["hh_brk"]) & (f["imb"] > p.squeeze_imb) & (f["clv"] > 0.6)
        sq_short = squeezed & vol_ok & (f["close"] < f["ll_brk"]) & (f["imb"] < -p.squeeze_imb) & (f["clv"] < 0.4)
        k, name = (-1, "FADE") if p.squeeze_mode == "fade" else (1, "SQUEEZE")
        put(sq_long, k, name, p.squeeze_sl_atr * a, p.squeeze_tp_atr * a)
        put(sq_short, -k, name, p.squeeze_sl_atr * a, p.squeeze_tp_atr * a)

    # 최소 손절폭/손익비 보정
    sl = np.maximum(sl, f["close"] * p.min_sl_pct).where(sig != 0)
    tp = np.maximum(tp, sl * p.min_rr).where(sig != 0)

    # EMA 추세 필터
    if p.trend_ema_len > 0:
        ema = f["close"].ewm(span=p.trend_ema_len, adjust=False).mean()
        against = ((sig == 1) & (f["close"] < ema)) | ((sig == -1) & (f["close"] > ema))
        sig[against] = 0
        setup[against] = ""

    # 펀딩비 필터: 롱이 과열(펀딩 높음)이면 롱 금지, 숏 과열이면 숏 금지
    if funding is not None and p.funding_block > 0:
        fr = funding.reindex(f.index).ffill().fillna(0.0)
        blocked = ((sig == 1) & (fr > p.funding_block)) | ((sig == -1) & (fr < -p.funding_block))
        sig[blocked] = 0
        setup[blocked] = ""

    # 워밍업 구간·ATR 미정 구간 제거
    invalid = a.isna() | (sl.isna() & (sig != 0))
    sig[invalid] = 0
    sig.iloc[: warmup_bars(p)] = 0

    f["signal"] = sig
    f["setup"] = setup.where(sig != 0, "")
    f["sl_dist"] = sl.where(sig != 0)
    f["tp_dist"] = tp.where(sig != 0)
    return f
