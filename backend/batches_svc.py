"""温控批次入库：温度点序列 JSON。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from attach_track import (
    attachment_status,
    counts_in_active,
    probe_lock_key,
    write_offline_ledger,
)
from db import connect, now_iso, write_audit
from locks import release, require_lock
from models import BatchIn
from validators import summarize_points, validate_temp_point

router = APIRouter(tags=["batches"])


def _decorate(row) -> dict:
    d = dict(row)
    d["attachment_status"] = attachment_status(
        d.get("probe_online_status", "在线"), d["status"]
    )
    d["counts_in_active"] = counts_in_active(
        d.get("probe_online_status", "在线"), d["status"]
    )
    return d


def _next_batch_code(conn, chamber_id: int) -> str:
    ch = conn.execute(
        "SELECT chamber_code FROM cold_chambers WHERE id=?", (chamber_id,)
    ).fetchone()
    prefix = ch["chamber_code"] if ch else f"CH-{chamber_id}"
    n = conn.execute(
        "SELECT COUNT(*) c FROM temp_batches WHERE chamber_id=?",
        (chamber_id,),
    ).fetchone()["c"]
    return f"TB-{prefix}-{n + 1:03d}"


@router.get("/api/batches")
def list_batches(authorization: AuthHeader = None, status: str | None = None):
    require_user(authorization)
    with connect() as conn:
        base = """SELECT b.*, c.chamber_code, p.probe_code,
                         p.online_status AS probe_online_status
                  FROM temp_batches b
                  JOIN cold_chambers c ON c.id=b.chamber_id
                  JOIN probes p ON p.id=b.probe_id"""
        if status:
            rows = conn.execute(
                base + " WHERE b.status=? ORDER BY b.id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(base + " ORDER BY b.id DESC").fetchall()
        items = []
        for r in rows:
            d = _decorate(r)
            d["temp_points"] = json.loads(d["temp_points"] or "[]")
            items.append(d)
        return {"items": items}


@router.get("/api/batches/{batch_id}")
def get_batch(batch_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            """SELECT b.*, c.name chamber_name, c.target_min, c.target_max,
                      p.probe_code, p.online_status AS probe_online_status
               FROM temp_batches b
               JOIN cold_chambers c ON c.id=b.chamber_id
               JOIN probes p ON p.id=b.probe_id WHERE b.id=?""",
            (batch_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "批次不存在")
        data = _decorate(row)
        data["temp_points"] = json.loads(data["temp_points"] or "[]")
        exc = conn.execute(
            "SELECT * FROM excursion_cursors WHERE batch_id=? ORDER BY id DESC",
            (batch_id,),
        ).fetchall()
        seals = conn.execute(
            "SELECT * FROM seal_snapshots WHERE batch_id=? ORDER BY id DESC",
            (batch_id,),
        ).fetchall()
        return {
            "batch": data,
            "excursions": [dict(e) for e in exc],
            "seals": [dict(s) for s in seals],
        }


@router.post("/api/batches")
def create_batch(body: BatchIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"chamber:{body.chamber_id}"
    with connect() as conn:
        # 先抢探头锁：双会话同探头抢挂时后到只记争用、零写
        probe = conn.execute(
            "SELECT * FROM probes WHERE id=?", (body.probe_id,)
        ).fetchone()
        if probe:
            probe_lk = probe_lock_key(body.probe_id)
            require_lock(conn, probe_lk, user)
        else:
            probe_lk = None
        try:
            require_lock(conn, lock_key, user)
            try:
                ch = conn.execute(
                    "SELECT * FROM cold_chambers WHERE id=?", (body.chamber_id,)
                ).fetchone()
                if not ch:
                    raise HTTPException(404, "仓间不存在")
                active = conn.execute(
                    """SELECT COUNT(*) c FROM temp_batches
                       WHERE chamber_id=? AND status NOT IN ('已封批','作废')""",
                    (body.chamber_id,),
                ).fetchone()["c"]
                if active >= int(ch["quota_batches"]):
                    raise HTTPException(409, "仓间在批配额已满")
                if not probe or probe["chamber_id"] != body.chamber_id:
                    raise HTTPException(422, "探头与仓间不匹配")
                if probe["online_status"] != "在线":
                    # 甲路径：离线探头挂批正式表零增、拒收，台账记离线偏航
                    write_offline_ledger(
                        conn,
                        user,
                        probe,
                        "挂批",
                        "离线偏航",
                        f"拒绝入正式批次表；批次编码={body.batch_code or '自动'}",
                    )
                    conn.commit()
                    raise HTTPException(409, "探头离线，禁止写批次")
                points = body.temp_points or []
                for pt in points:
                    validate_temp_point(
                        float(pt.get("c", pt.get("temp", 0))),
                        float(ch["target_min"]),
                        float(ch["target_max"]),
                    )
                tmin, tmax = summarize_points(points)
                code = body.batch_code or _next_batch_code(conn, body.chamber_id)
                if conn.execute(
                    "SELECT id FROM temp_batches WHERE batch_code=?", (code,)
                ).fetchone():
                    raise HTTPException(409, "批次编码已存在")
                status = "已入库" if points else "草稿"
                now = now_iso()
                cur = conn.execute(
                    """INSERT INTO temp_batches(batch_code,chamber_id,probe_id,cargo_ref,
                       temp_points,generation,status,min_observed,max_observed,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        code,
                        body.chamber_id,
                        body.probe_id,
                        body.cargo_ref,
                        json.dumps(points, ensure_ascii=False),
                        1,
                        status,
                        tmin if points else None,
                        tmax if points else None,
                        now,
                    ),
                )
                write_audit(
                    conn,
                    user,
                    "batch_inbound",
                    code,
                    {"batch_id": cur.lastrowid, "point_count": len(points)},
                )
                conn.commit()
                return {
                    "id": cur.lastrowid,
                    "batch_code": code,
                    "status": status,
                    "point_count": len(points),
                    "attachment_status": "正式在批",
                }
            finally:
                release(conn, lock_key, user)
        finally:
            if probe_lk:
                release(conn, probe_lk, user)