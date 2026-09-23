"""
Wrapper HistoricalStore yang terintegrasi penuh dengan UnifiedDatabase (core/database.py).
Menjaga kompatibilitas mundur 100% untuk modul dan unit test yang mengimpor historical_store.
"""

from typing import List, Dict, Any, Optional
from core.database import db, UnifiedDatabase
from core.models import TakerSnapshot

class HistoricalStore:
    def __init__(self, db_path: Optional[str] = None, db_instance: Optional[UnifiedDatabase] = None):
        if db_instance:
            self.db = db_instance
        elif db_path:
            self.db = UnifiedDatabase(sqlite_path=db_path)
        else:
            self.db = db

    def _get_connection(self):
        return self.db._get_connection()

    def record_funding_rates(self, symbol: str, history: List[Dict[str, Any]], interval_hours: int = 8) -> int:
        return self.db.record_funding_rates(symbol, history, interval_hours)

    def get_funding_history(self, symbol: str, days: int = 30) -> List[Dict[str, Any]]:
        return self.db.get_funding_history(symbol, days)

    def record_taker_snapshot(self, snapshot: TakerSnapshot):
        return self.db.record_taker_snapshot(snapshot)

    def record_agi_experience(
        self,
        event_type: str,
        base_asset: str,
        funding_rate: Optional[float] = None,
        harvest_usdt: Optional[float] = None,
        net_pnl_usdt: Optional[float] = None,
        holding_hours: Optional[float] = None,
        was_bep_reached: bool = False,
        ai_decision: Optional[str] = None,
        lesson_learned: Optional[str] = None,
        tactical_rule: Optional[str] = None,
        pair_reputation_score: float = 0.0
    ):
        return self.db.record_agi_experience(
            event_type=event_type,
            base_asset=base_asset,
            funding_rate=funding_rate,
            harvest_usdt=harvest_usdt,
            net_pnl_usdt=net_pnl_usdt,
            holding_hours=holding_hours,
            was_bep_reached=was_bep_reached,
            ai_decision=ai_decision,
            lesson_learned=lesson_learned,
            tactical_rule=tactical_rule,
            pair_reputation_score=pair_reputation_score
        )

    def get_pair_reputation(self, base_asset: str) -> Dict[str, Any]:
        return self.db.get_pair_reputation(base_asset)

    def get_all_pair_reputations(self) -> Dict[str, float]:
        return self.db.get_all_pair_reputations()

    def prune_older_than_days(self, days: int = 30) -> Dict[str, int]:
        return self.db.prune_older_than_days(days)

historical_store = HistoricalStore()
