from decimal import Decimal

from app.engine import CopyEngine
from app.models import CopyTrade, PaperOrder
from app.polymarket import LeaderActivity


class RecordingSession:
    def __init__(self):
        self.items = []

    def add(self, item):
        self.items.append(item)


def test_skipped_copy_is_always_visible_as_rejected_order():
    session = RecordingSession()
    trade = CopyTrade(id=7)
    event = LeaderActivity(
        event_key="event",
        timestamp=1,
        condition_id="condition",
        token_id="token",
        side="BUY",
        size=Decimal("10"),
        price=Decimal("0.5"),
        title="Market",
        outcome="Yes",
        slug="market",
    )

    CopyEngine.record_rejection(session, trade, event, "below_min_copy_notional")

    assert len(session.items) == 1
    order = session.items[0]
    assert isinstance(order, PaperOrder)
    assert order.copy_trade_id == 7
    assert order.status == "rejected"
    assert order.reason == "below_min_copy_notional"
