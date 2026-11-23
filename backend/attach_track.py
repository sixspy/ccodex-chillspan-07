"""离线探头挂批分轨口径（甲路径）。

口径：探头离线则挂批正式表零增、拒收、批次挂接零改；影子残留须标为
「离线未晋升」，不得计入在线在批，也不得封批洗白。混轨禁止。
立场按日写入离线入批台账并当日封口。
"""
from __future__ import annotations

from datetime import date

from db import now_iso

OFFLINE = "离线"
FORMAL_ACTIVE = "正式在批"
OFFLINE_PENDING = "离线未晋升"
SEALED = "已封批"
VOID = "作废"

LEDGER_STANCE = (
    "离线探头挂批分轨（甲路径）：探头离线→挂批正式表零增、拒收、批次挂接零改；"
    "既有影子挂接一律标「离线未晋升」，不计入在线在批，须待探头上线后原子晋升，"
    "否则影子作废。混轨禁止，偏离台账记离线偏航并拒写。"
)

ACTIVE_SQL = "status NOT IN ('已封批','作废')"


def probe_lock_key(probe_id: int) -> str:
    return f"probe:{probe_id}"


def attachment_status(probe_online_status: str, batch_status: str) -> str:
    if batch_status in (SEALED, VOID):
        return batch_status
    if probe_online_status != "在线":
        return OFFLINE_PENDING
    return FORMAL_ACTIVE


def counts_in_active(probe_online_status: str, batch_status: str) -> bool:
    return batch_status not in (SEALED, VOID) and probe_online_status == "在线"


def probe_track_status(probe_online_status: str) -> str:
    """探头自身轨：在线采样 vs 离线未晋升（用于校准等以探头为主体的读面）。"""
    return "在线采样" if probe_online_status == "在线" else OFFLINE_PENDING


def active_batches_for_probe(conn, probe_id: int) -> int:
    return conn.execute(
        f"SELECT COUNT(*) c FROM temp_batches WHERE probe_id=? AND {ACTIVE_SQL}",
        (probe_id,),
    ).fetchone()["c"]


def offline_attached_for_probe(conn, probe_id: int) -> int:
    return conn.execute(
        f"""SELECT COUNT(*) c FROM temp_batches b
            JOIN probes p ON p.id=b.probe_id
            WHERE b.probe_id=? AND p.online_status='离线' AND b.{ACTIVE_SQL}""",
        (probe_id,),
    ).fetchone()["c"]


def ledger_date() -> str:
    return date.today().isoformat()


def seal_ledger_day(conn) -> dict:
    day = ledger_date()
    conn.execute(
        """INSERT OR IGNORE INTO offline_ledger_days(ledger_date,stance,sealed_at)
           VALUES (?,?,?)""",
        (day, LEDGER_STANCE, now_iso()),
    )
    row = conn.execute(
        "SELECT * FROM offline_ledger_days WHERE ledger_date=?", (day,)
    ).fetchone()
    return dict(row)


def write_offline_ledger(conn, actor: str, probe, action: str, verdict: str, detail: str = "") -> int:
    """写离线入批台账并封当日口；probe 为 probes 行。"""
    day = ledger_date()
    cur = conn.execute(
        """INSERT INTO offline_ledger(ledger_date,probe_id,probe_code,action,
           verdict,detail,actor,created_at) VALUES (?,?,?,?,?,?,?,?)""",
        (
            day,
            probe["id"],
            probe["probe_code"],
            action,
            verdict,
            detail,
            actor,
            now_iso(),
        ),
    )
    seal_ledger_day(conn)
    return cur.lastrowid
