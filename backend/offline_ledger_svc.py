"""离线入批台账：甲路径立场按日登记并当日封口。"""
from __future__ import annotations

from fastapi import APIRouter

from attach_track import LEDGER_STANCE, ledger_date, seal_ledger_day
from auth import AuthHeader, require_user
from db import connect

router = APIRouter(tags=["offline-ledger"])


@router.get("/api/offline-ledger")
def list_offline_ledger(
    authorization: AuthHeader = None, ledger_date_filter: str | None = None
):
    require_user(authorization)
    day = ledger_date_filter or ledger_date()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM offline_ledger_days WHERE ledger_date=?", (day,)
        ).fetchone()
        if row is None:
            # 只读访问也保证当日封口：立场随日固定，不受后续取巧改写
            row = seal_ledger_day(conn)
            conn.commit()
        items = conn.execute(
            """SELECT * FROM offline_ledger WHERE ledger_date=?
               ORDER BY id DESC""",
            (day,),
        ).fetchall()
        return {
            "ledger_date": row["ledger_date"],
            "stance": row["stance"],
            "sealed": True,
            "sealed_at": row["sealed_at"],
            "items": [dict(x) for x in items],
            "summary": {
                "total": len(items),
                "offline_drift": sum(
                    1 for x in items if x["verdict"] == "离线偏航"
                ),
            },
        }


__all__ = ["router", "LEDGER_STANCE"]
