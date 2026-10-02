"""머신러닝 예측 실험.

과거 지표(특징)로 '롱/숏으로 들어가면 손절보다 익절에 먼저 닿을 확률'을 학습하고,
확률이 높을 때만 진입한다. 학습은 항상 '테스트 구간보다 과거' 데이터로만 하는
워크포워드(walk-forward) 방식이라, 백테스트에 미래 정보가 섞이지 않는다.
"""
import numpy as np
import pandas as pd

from . import classic
from . import indicators as ind

SL_ATR, TP_ATR = 1.5, 3.0  # 하이킨아시 전략과 같은 손익 구조 (1 : 2)


def features(df: pd.DataFrame, btc: pd.DataFrame | None = None) -> pd.DataFrame:
    """모든 특징은 해당 봉 마감 시점까지의 정보만 사용한다."""
    c, v = df["close"], df["volume"]
    a = ind.atr(df, 14)
    x = pd.DataFrame(index=df.index)
    for k in (1, 3, 6, 12, 24, 48):
        x[f"ret_{k}"] = c.pct_change(k)
    x["atr_pct"] = a / c
    x["vol_ratio"] = a / a.rolling(100).mean()
    x["rsi14"] = classic.rsi(c, 14)
    x["rsi2"] = classic.rsi(c, 2)
    macd = classic.ema(c, 12) - classic.ema(c, 26)
    x["macd_hist"] = (macd - macd.ewm(span=9, adjust=False).mean()) / a
    for n in (20, 50, 200):
        x[f"dist_ema{n}"] = (c - classic.ema(c, n)) / a
    mid, sd = c.rolling(20).mean(), c.rolling(20).std()
    x["bb_pctb"] = (c - (mid - 2 * sd)) / (4 * sd)
    x["bb_width"] = 4 * sd / mid
    ho, _, _, hc = classic.heikin_ashi(df)
    bull = (hc > ho).astype(int)
    x["ha_bull"] = bull
    x["ha_streak"] = bull.groupby((bull != bull.shift()).cumsum()).cumcount() + 1
    x["vol_z"] = ind.log_volume_z(df, 96)
    imb = ind.taker_imbalance(df)
    x["imb_1"] = imb
    x["imb_6"] = imb.rolling(6).mean()
    x["clv"] = ind.close_location(df)
    x["adx"] = ind.adx(df, 14)
    x["hour"] = df.index.hour
    if btc is not None:
        bc = btc["close"].reindex(df.index).ffill()
        for k in (1, 6, 24):
            x[f"btc_ret_{k}"] = bc.pct_change(k)
        x["btc_dist_ema200"] = (bc - classic.ema(bc, 200)) / ind.atr(btc, 14).reindex(df.index).ffill()
    return x


def labels(df: pd.DataFrame, max_hold: int = 36) -> pd.DataFrame:
    """다음 봉 시가 진입 시, 롱/숏 각각 익절(TP)에 손절(SL)보다 먼저 닿았는지 (1/0).

    같은 봉에서 둘 다 닿으면 손절로 본다(보수적). 기간 내 둘 다 안 닿으면 0.
    마지막 max_hold 봉은 결과를 알 수 없으므로 NaN.
    """
    o, h, l = (df[k].to_numpy() for k in ("open", "high", "low"))
    a = ind.atr(df, 14).to_numpy()
    n = len(df)
    out = {"y_long": np.full(n, np.nan), "y_short": np.full(n, np.nan)}
    for d, key in ((1, "y_long"), (-1, "y_short")):
        first_tp = np.full(n, np.inf)
        first_sl = np.full(n, np.inf)
        entry = np.r_[o[1:], np.nan]
        sl = entry - d * SL_ATR * a
        tp = entry + d * TP_ATR * a
        for k in range(1, max_hold + 1):
            hk = np.r_[h[k:], np.full(k, np.nan)]
            lk = np.r_[l[k:], np.full(k, np.nan)]
            hit_tp = (hk >= tp) if d == 1 else (lk <= tp)
            hit_sl = (lk <= sl) if d == 1 else (hk >= sl)
            first_tp = np.where(hit_tp & np.isinf(first_tp), k, first_tp)
            first_sl = np.where(hit_sl & np.isinf(first_sl), k, first_sl)
        y = (first_tp < first_sl).astype(float)
        y[n - max_hold - 1:] = np.nan
        y[np.isnan(a)] = np.nan
        out[key] = y
    return pd.DataFrame(out, index=df.index)


def build_dataset(data: dict[str, pd.DataFrame], btc_symbol: str = "BTCUSDT", max_hold: int = 36) -> pd.DataFrame:
    btc = data.get(btc_symbol)
    frames = []
    for s, df in data.items():
        x = features(df, btc)
        y = labels(df, max_hold)
        z = pd.concat([x, y], axis=1)
        z["symbol"] = s
        z["atr"] = ind.atr(df, 14)
        frames.append(z)
    out = pd.concat(frames)
    out.index.name = "time"
    return out.reset_index().sort_values(["time", "symbol"]).reset_index(drop=True)


def walk_forward_predict(ds: pd.DataFrame, feat_cols: list[str], test_start: pd.Timestamp,
                         n_folds: int = 4, embargo: pd.Timedelta = pd.Timedelta(days=7), seed: int = 0):
    """test_start 이후를 n_folds 구간으로 나눠, 각 구간은 그 이전 데이터로만 학습한 모델로 예측."""
    from sklearn.ensemble import HistGradientBoostingClassifier

    t = ds["time"]
    edges = pd.date_range(test_start, t.max(), periods=n_folds + 1)
    preds = pd.DataFrame(np.nan, index=ds.index, columns=["p_long", "p_short"])
    for i in range(n_folds):
        lo, hi = edges[i], edges[i + 1]
        train = ds[t < lo - embargo]  # 라벨이 테스트 구간으로 넘어가지 않도록 간격
        test_mask = (t >= lo) & ((t < hi) if i < n_folds - 1 else (t <= hi))
        for col, key in (("y_long", "p_long"), ("y_short", "p_short")):
            tr = train.dropna(subset=[col])
            model = HistGradientBoostingClassifier(
                max_iter=200, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=200,
                l2_regularization=1.0, random_state=seed)
            model.fit(tr[feat_cols], tr[col].astype(int))
            preds.loc[test_mask, key] = model.predict_proba(ds.loc[test_mask, feat_cols])[:, 1]
    return preds
