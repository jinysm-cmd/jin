import numpy as np
import pandas as pd
import pytest

from bot import ml
from tests.test_strategy import synthetic

pytest.importorskip("sklearn")


def test_features_no_lookahead():
    df = synthetic(n=3000, seed=11)
    full = ml.features(df, df)
    part = ml.features(df.iloc[:2000], df.iloc[:2000])
    pd.testing.assert_frame_equal(full.iloc[:2000], part)


def test_labels_simple_case():
    idx = pd.date_range("2026-01-01", periods=60, freq="4h", tz="UTC")
    px = np.full(60, 100.0)
    df = pd.DataFrame({"open": px, "high": px + 1, "low": px - 1, "close": px,
                       "volume": 1.0, "taker_buy_volume": 0.5}, index=idx)
    # ATR=2 → 롱 TP=+6, SL=-3. 30번째 봉 이후 급등시키면 그 직전 진입은 익절
    df.loc[idx[31]:, ["open", "high", "low", "close"]] += 20
    y = ml.labels(df, max_hold=10)
    assert y["y_long"].iloc[29] == 1.0
    assert y["y_short"].iloc[29] == 0.0
    assert y["y_long"].iloc[-5:].isna().all()


def test_no_leakage_on_random_data():
    """무작위 데이터에서는 예측력이 없어야 한다 (AUC≈0.5). 높게 나오면 미래 정보가 새는 것."""
    from sklearn.metrics import roc_auc_score

    data = {f"S{i}USDT": synthetic(n=4000, seed=100 + i) for i in range(4)}
    data["BTCUSDT"] = synthetic(n=4000, seed=99)
    ds = ml.build_dataset(data)
    cols = [c for c in ds.columns if c not in ("time", "symbol", "atr", "y_long", "y_short")]
    start = ds["time"].min() + (ds["time"].max() - ds["time"].min()) * 0.5
    ds = pd.concat([ds, ml.walk_forward_predict(ds, cols, start, n_folds=2, embargo=pd.Timedelta(hours=4))], axis=1)
    te = ds[ds["time"] >= start].dropna(subset=["y_long", "p_long"])
    auc = roc_auc_score(te["y_long"], te["p_long"])
    assert 0.44 < auc < 0.56, auc
