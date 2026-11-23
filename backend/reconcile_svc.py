"""仓间配额 vs 在批差额对账 + 互证只读（离线挂接分轨另计）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from attach_track import ACTIVE_SQL, attachment_status, counts_in_active
from auth import AuthHeader, require_user
from db import connect

router = APIRouter(tags=["reconcile"])


def _chamber_recon(conn, chamber) -> dict:
    cid = chamber["id"]
    quota = int(chamber["quota_batches"])
    rows = conn.execute(
        f"""SELECT p.online_status, COUNT(*) c FROM temp_batches b
            JOIN probes p ON p.id=b.probe_id
            WHERE b.chamber_id=? AND b.{ACTIVE_SQL}
            GROUP BY p.online_status""",
        (cid,),
    ).fetchall()
    split = {r["online_status"]: r["c"] for r in rows}
    online_active = int(split.get("在线", 0))
    offline_attached = int(split.get("离线", 0))
    formal_active = online_active + offline_attached
    sealed = conn.execute(
        """SELECT COUNT(*) c FROM temp_batches
           WHERE chamber_id=? AND status='已封批'""",
        (cid,),
    ).fetchone()["c"]
    gap = quota - formal_active
    # 离线挂接不属于在线在批：有影子残留即失衡，须待晋升或作废
    ok = online_active <= quota and offline_attached == 0
    return {
        "chamber_id": cid,
        "chamber_code": chamber["chamber_code"],
        "region": chamber["region"],
        "quota_batches": quota,
        "active_batches": online_active,
        "formal_active_batches": formal_active,
        "offline_attached_batches": offline_attached,
        "sealed_batches": sealed,
        "quota_left": gap,
        "balanced": ok,
        "proof": {
            "formula": "online_active <= quota_batches and offline_attached == 0",
            "left": online_active,
            "right": quota,
            "offline_attached": offline_attached,
        },
    }


@router.get("/api/reconcile")
def reconcile_all(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        chambers = conn.execute("SELECT * FROM cold_chambers ORDER BY id").fetchall()
        items = [_chamber_recon(conn, c) for c in chambers]
        bad = [i for i in items if not i["balanced"]]
        return {
            "items": items,
            "summary": {
                "total": len(items),
                "balanced": len(items) - len(bad),
                "unbalanced": len(bad),
            },
        }


@router.get("/api/reconcile/{chamber_code}")
def reconcile_one(chamber_code: str, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        ch = conn.execute(
            "SELECT * FROM cold_chambers WHERE chamber_code=?", (chamber_code,)
        ).fetchone()
        if not ch:
            raise HTTPException(404, "仓间不存在")
        return _chamber_recon(conn, ch)


@router.get("/api/proof/batch/{batch_code}")
def batch_proof(batch_code: str, authorization: AuthHeader = None):
    """批次互证：封批代 + 温谱导出 + 开放游标。"""
    require_user(authorization)
    with connect() as conn:
        batch = conn.execute(
            "SELECT * FROM temp_batches WHERE batch_code=?", (batch_code,)
        ).fetchone()
        if not batch:
            raise HTTPException(404, "批次不存在")
        open_exc = conn.execute(
            """SELECT COUNT(*) c FROM excursion_cursors
               WHERE batch_id=? AND status='开放'""",
            (batch["id"],),
        ).fetchone()["c"]
        exports = conn.execute(
            """SELECT COUNT(*) c FROM spectrum_exports
               WHERE batch_id=? AND status='已导出'""",
            (batch["id"],),
        ).fetchone()["c"]
        sealed = batch["status"] == "已封批"
        spectrum_ok = (exports >= 1) if sealed else True
        probe = conn.execute(
            "SELECT online_status FROM probes WHERE id=?", (batch["probe_id"],)
        ).fetchone()
        online = probe["online_status"] if probe else "在线"
        return {
            "batch_code": batch_code,
            "status": batch["status"],
            "generation": batch["generation"],
            "attachment_status": attachment_status(online, batch["status"]),
            "counts_in_active": counts_in_active(online, batch["status"]),
            "open_excursions": open_exc,
            "spectrum_exports": exports,
            "sealed": sealed,
            "excursion_closed": open_exc == 0,
            "spectrum_matches_seal": spectrum_ok,
            "proof": {
                "formula": "sealed => spectrum>=1 and open_excursions==0",
                "sealed": sealed,
                "open_excursions": open_exc,
                "exports": exports,
            },
        }