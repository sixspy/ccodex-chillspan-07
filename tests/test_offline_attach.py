"""离线探头挂批分轨红线测（甲路径）。

三条先红后绿：
  甲 离线探头不得进入正式批次表，且不得在探头仍有在批批次时置离线。
  乙 未晋升的离线挂接不得在批次/仓间/探头读面上显示为在批。
  争用 双会话对同一离线探头抢挂须走探头锁，后到只记争用、零写。
"""
import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str = "admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _probes(c: TestClient, h: dict) -> list:
    return c.get("/api/probes", headers=h).json()["items"]


def _chamber(c: TestClient, h: dict, code: str) -> dict:
    return next(
        x for x in c.get("/api/chambers", headers=h).json()["items"]
        if x["chamber_code"] == code
    )


def _batch_payload(chamber_id: int, probe_id: int, code: str) -> dict:
    return {
        "chamber_id": chamber_id,
        "probe_id": probe_id,
        "cargo_ref": "离线挂批红线货载",
        "temp_points": [{"t": "08:00", "c": -21.0}],
        "batch_code": code,
    }


def test_offline_probe_never_formally_attached():
    c = TestClient(app)
    h = _auth(c)
    ch = _chamber(c, h, "CK-HB-02")
    probes = _probes(c, h)
    online = next(
        p for p in probes
        if p["chamber_code"] == "CK-HB-02" and p["online_status"] == "在线"
    )
    offline = next(p for p in probes if p["probe_code"] == "PR-HB-B2")
    assert offline["online_status"] == "离线"

    # 离线探头直挂正式批次：拒收，正式表零增，台账记离线偏航
    before = len(c.get("/api/batches", headers=h).json()["items"])
    r = c.post(
        "/api/batches",
        headers=h,
        json=_batch_payload(ch["id"], offline["id"], "TB-OFFLINE-DIRECT"),
    )
    assert r.status_code == 409
    assert len(c.get("/api/batches", headers=h).json()["items"]) == before

    # 在线探头已有在批批次时置离线：拒收，批次挂接零改
    created = c.post(
        "/api/batches",
        headers=h,
        json=_batch_payload(ch["id"], online["id"], "TB-ONLINE-THEN-OFF"),
    )
    assert created.status_code == 200
    batch_id = created.json()["id"]
    r2 = c.post(f"/api/probes/{online['id']}/offline", headers=h)
    assert r2.status_code == 409
    assert (
        c.get(f"/api/probes/{online['id']}", headers=h).json()["probe"][
            "online_status"
        ]
        == "在线"
    )
    assert (
        c.get(f"/api/batches/{batch_id}", headers=h).json()["batch"]["status"]
        == "已入库"
    )

    ledger = c.get("/api/offline-ledger", headers=h)
    assert ledger.status_code == 200
    items = ledger.json()["items"]
    assert any(
        e["probe_code"] == offline["probe_code"]
        and e["action"] == "挂批"
        and e["verdict"] == "离线偏航"
        for e in items
    )
    assert any(
        e["probe_id"] == online["id"]
        and e["action"] == "置离线"
        and e["verdict"] == "离线偏航"
        for e in items
    )


def test_pending_offline_attachment_not_shown_as_in_batch():
    c = TestClient(app)
    h = _auth(c)
    ch = _chamber(c, h, "CK-HB-02")
    offline = next(p for p in _probes(c, h) if p["probe_code"] == "PR-HB-B2")

    before_batches = len(c.get("/api/batches", headers=h).json()["items"])
    before_ch = c.get(f"/api/chambers/{ch['id']}", headers=h).json()

    # 离线挂批被拒，只留未晋升台账意图，不得进入正式在批
    r = c.post(
        "/api/batches",
        headers=h,
        json=_batch_payload(ch["id"], offline["id"], "TB-PENDING-OFFLINE"),
    )
    assert r.status_code == 409
    assert len(c.get("/api/batches", headers=h).json()["items"]) == before_batches
    after_ch = c.get(f"/api/chambers/{ch['id']}", headers=h).json()
    assert after_ch["active_batch_count"] == before_ch["active_batch_count"]

    ledger = c.get("/api/offline-ledger", headers=h).json()["items"]
    pending = [
        e for e in ledger
        if e["probe_id"] == offline["id"] and e["action"] == "挂批"
    ]
    assert pending and all(e["verdict"] == "离线偏航" for e in pending)

    # 模拟一条历史未晋升挂接（影子残留），读面必须标为离线未晋升、不计入在线在批
    with connect() as conn:
        conn.execute(
            """INSERT INTO temp_batches(batch_code,chamber_id,probe_id,cargo_ref,
               temp_points,generation,status,min_observed,max_observed,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                "TB-SHADOW-001",
                ch["id"],
                offline["id"],
                "未晋升影子货载",
                '[{"t":"08:00","c":-21.0}]',
                1,
                "已入库",
                -21.0,
                -21.0,
                "2026-11-23T00:00:00",
            ),
        )
        conn.commit()

    items = c.get("/api/batches", headers=h).json()["items"]
    row = next(x for x in items if x["batch_code"] == "TB-SHADOW-001")
    assert row["attachment_status"] == "离线未晋升"
    assert row["counts_in_active"] is False

    ch_detail = c.get(f"/api/chambers/{ch['id']}", headers=h).json()
    assert ch_detail["offline_attached_count"] >= 1
    assert (
        ch_detail["online_active_batch_count"]
        == ch_detail["active_batch_count"] - ch_detail["offline_attached_count"]
    )

    pr = next(p for p in _probes(c, h) if p["id"] == offline["id"])
    assert pr["offline_attached"] >= 1

    # 校准页：该探头的校准记录须同态标注离线未晋升，不得当作在线采样
    cal = c.post(
        "/api/calibrations",
        headers=h,
        json={
            "probe_id": offline["id"],
            "offset_c": 0.2,
            "valid_until": "2027-01-01",
            "officer": "夜班质检",
        },
    )
    assert cal.status_code == 200
    cals = c.get(
        f"/api/calibrations?probe_id={offline['id']}", headers=h
    ).json()["items"]
    assert cals[0]["attachment_status"] == "离线未晋升"
    assert cals[0]["offline_attached"] >= 1

    # 对账页：离线挂接另计，不计入在线在批，且并列为失衡/不实挂批
    recon = c.get("/api/reconcile/CK-HB-02", headers=h).json()
    assert recon["offline_attached_batches"] >= 1
    assert recon["active_batches"] == (
        recon["formal_active_batches"] - recon["offline_attached_batches"]
    )
    assert recon["offline_attached_batches"] >= 1
    assert recon["balanced"] is False

    proof = c.get("/api/proof/batch/TB-SHADOW-001", headers=h).json()
    assert proof["attachment_status"] == "离线未晋升"
    assert proof["counts_in_active"] is False

    # 离线未晋升批次不得封批洗白
    seal = c.post("/api/seals", headers=h, json={"batch_id": row["id"]})
    assert seal.status_code == 409

    ledger_json = c.get("/api/offline-ledger", headers=h).json()
    assert "离线" in ledger_json["stance"]
    assert ledger_json["sealed"] is True
    assert ledger_json["ledger_date"] == __import__("datetime").date.today().isoformat()


def test_two_sessions_contend_on_probe_lock():
    c_admin = TestClient(app)
    c_ops = TestClient(app)
    h_admin = _auth(c_admin, "admin")
    h_ops = _auth(c_ops, "ops")
    ch = _chamber(c_admin, h_admin, "CK-HB-02")
    offline = next(
        p for p in _probes(c_admin, h_admin) if p["probe_code"] == "PR-HB-B2"
    )
    lock_key = f"probe:{offline['id']}"

    assert (
        c_admin.post(
            "/api/locks/acquire",
            headers=h_admin,
            json={"lock_key": lock_key},
        ).status_code
        == 200
    )
    before = len(c_admin.get("/api/batches", headers=h_admin).json()["items"])
    r = c_ops.post(
        "/api/batches",
        headers=h_ops,
        json=_batch_payload(ch["id"], offline["id"], "TB-CONTEND-OFFLINE"),
    )
    assert r.status_code == 409
    assert "争用" in r.json()["detail"]
    assert (
        len(c_admin.get("/api/batches", headers=h_admin).json()["items"]) == before
    )
    cont = c_admin.get("/api/contention", headers=h_admin).json()["items"]
    assert any(x["lock_key"] == lock_key and x["actor"] == "ops" for x in cont)
