"""포지션 사이징: '손절 시 계좌의 risk_per_trade 만큼만 잃도록' 수량을 정한다."""


def notional_fraction(entry: float, sl_dist: float, risk_per_trade: float, leverage: float, max_positions: int,
                      margin_per_trade: float = 0.0) -> float:
    """계좌 대비 포지션 명목가 비율.

    - 기본(손절폭 기준 사이징): risk / (손절폭%)
    - margin_per_trade > 0 이면 고정 증거금 방식: 계좌의 margin_per_trade 를 증거금으로 × 레버리지
    - 상한: 동시 최대 포지션을 모두 열어도 증거금이 계좌를 넘지 않도록 leverage / max_positions
    """
    if entry <= 0 or sl_dist <= 0:
        return 0.0
    if margin_per_trade > 0:
        return min(margin_per_trade, 1.0 / max(1, max_positions)) * leverage
    by_risk = risk_per_trade / (sl_dist / entry)
    cap = leverage / max(1, max_positions)
    return min(by_risk, cap)
