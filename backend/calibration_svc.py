"""探头校准记录。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from attach_track import offline_attached_for_probe, probe_track_status
from db import connect, now_iso, write_audit
from models import CalibrationIn

router = APIRouter(tags=["calibration"])


def _decorate(conn, row) -> dict:
    d = dict(row)
    d["attachment_status"] = probe_track_status(d["online_status"])
    d["offline_attached"] = offline_attached_for_probe(conn, d["probe_id"])
    return d


@router.get("/api/calibrations")
def list_calibrations(authorization: AuthHeader = None, probe_id: int | None = None):
    require_user(authorization)
    with connect() as conn:
        base = """SELECT c.*, p.probe_code, p.online_status
                  FROM calibration_records c JOIN probes p ON p.id=c.probe_id"""
        if probe_id:
            rows = conn.execute(
                base + " WHERE c.probe_id=? ORDER BY c.id DESC", (probe_id,)
            ).fetchall()
        else:
            rows = conn.execute(base + " ORDER BY c.id DESC").fetchall()
        return {"items": [_decorate(conn, r) for r in rows]}


@router.post("/api/calibrations")
def add_calibration(body: CalibrationIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        probe = conn.execute(
            "SELECT * FROM probes WHERE id=?", (body.probe_id,)
        ).fetchone()
        if not probe:
            raise HTTPException(404, "探头不存在")
        cur = conn.execute(
            """INSERT INTO calibration_records(probe_id,calibrated_at,offset_c,
               valid_until,officer,status) VALUES (?,?,?,?,?,?)""",
            (
                body.probe_id,
                now_iso(),
                body.offset_c,
                body.valid_until,
                body.officer,
                "有效",
            ),
        )
        conn.execute(
            "UPDATE probes SET calibration_due=? WHERE id=?",
            (body.valid_until, body.probe_id),
        )
        write_audit(
            conn,
            user,
            "calibration_add",
            probe["probe_code"],
            {"cal_id": cur.lastrowid, "valid_until": body.valid_until},
        )
        conn.commit()
        return {
            "id": cur.lastrowid,
            "probe_code": probe["probe_code"],
            "valid_until": body.valid_until,
        }