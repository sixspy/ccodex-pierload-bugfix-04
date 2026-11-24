"""BUGFIX-04 轴改修复第19号 · 三红回归（轴改修丁）。

修复纪要（当班封死，甲路径）：
- 封闸代已生效（票态=已封闸，或存在与当前闸代一致的已封闸快照）→ 正式轴重零改，
  过磅更新口一律 409 拒写；不设影子轴重草案（乙路径不与甲并存）。
- 同一已封票并发回写先抢 ``ticket:{id}`` 票锁，后到只记争用痕，不写业务。
- 血缘不得登记封后轴重修订边；对账/快照须以快照毛重与正式毛重同态为准，
  快照错位不得判绿。

三红：
1. 甲：已封票回写轴重仍被受理，正式轴重被改。
2. 乙：回写造成快照毛重与正式毛重错位，且影子修订混入血缘。
3. 争用：双会话同票改轴未被票锁拦下，无争用痕。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import app
from db import init_db

SEALED_CODE = "WT-LN-N-02-IN-001"
SEALED_SEAL_REF = "GS-WT-LN-N-02-IN-001-G2"
NEW_AXLES = [{"axle": 1, "w": 30.0}, {"axle": 2, "w": 30.0}]


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str = "admin", password: str = "admin123") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _sealed_ticket(c: TestClient, headers: dict) -> dict:
    items = c.get("/api/tickets?status=已封闸", headers=headers).json()["items"]
    return next(t for t in items if t["ticket_code"] == SEALED_CODE)


def _manifest_gross(manifest: str) -> float:
    for part in (manifest or "").split(";"):
        if part.startswith("毛重=") and part.endswith("t"):
            return float(part[3:-1])
    raise AssertionError(f"快照清单缺毛重: {manifest!r}")


# ---------------------------------------------------------------- 三红之甲
def test_red_jia_sealed_ticket_axle_write_rejected():
    """甲路径：封闸代已生效后，正式轴重零改，回写必须拒写。"""
    c = TestClient(app)
    h = _auth(c)
    ticket = _sealed_ticket(c, h)
    before = c.get(f"/api/tickets/{ticket['id']}", headers=h).json()["ticket"]

    res = c.post(
        f"/api/tickets/{ticket['id']}/axles",
        headers=h,
        json={"axle_weights": NEW_AXLES},
    )
    assert res.status_code == 409, (
        f"甲点破：已封票轴重仍被回写（HTTP {res.status_code}，{res.text}）"
    )
    assert "封后禁改" in res.json()["detail"]

    after = c.get(f"/api/tickets/{ticket['id']}", headers=h).json()["ticket"]
    assert after["gross_t"] == before["gross_t"], "正式毛重被改"
    assert after["axle_weights"] == before["axle_weights"], "正式轴重被改"

    seal = c.get(f"/api/seals/{SEALED_SEAL_REF}", headers=h).json()
    assert _manifest_gross(seal["manifest"]) == float(after["gross_t"]), "快照与正式毛重错位"


# ---------------------------------------------------------------- 三红之乙
def test_red_yi_no_shadow_draft_leaks_into_lineage():
    """乙路径不设：封后修订不得留下未晋升的影子草案，更不得混入血缘。"""
    c = TestClient(app)
    h = _auth(c)
    ticket = _sealed_ticket(c, h)

    # 以监控台回写口尝试封后修订
    c.post(
        f"/api/tickets/{ticket['id']}/axles",
        headers=h,
        json={"axle_weights": NEW_AXLES},
    )

    # 快照毛重（清单）必须与正式毛重同态；错位即坐实影子草案
    seal = c.get(f"/api/seals/{SEALED_SEAL_REF}", headers=h).json()
    formal = c.get(f"/api/tickets/{ticket['id']}", headers=h).json()["ticket"]
    assert _manifest_gross(seal["manifest"]) == float(formal["gross_t"]), (
        "乙坐实：快照毛重与正式毛重错位，封后修订以影子草案状态存在"
    )

    # 封后轴重修订边（影子草案晋升前的痕迹）不得写入血缘
    edge = c.post(
        "/api/lineage",
        headers=h,
        json={
            "from_ticket_id": ticket["id"],
            "edge_type": "轴重修订",
            "detail": "封后回写轴重（影子草案未晋升）",
        },
    )
    assert edge.status_code == 409, (
        f"乙坐实：影子草案未晋升却混入血缘（HTTP {edge.status_code}，{edge.text}）"
    )


# ---------------------------------------------------------------- 三红之争用
def test_red_contention_two_sessions_same_sealed_axle():
    """争用：双会话同打一张已封票改轴，须抢票锁，后到只记争用痕。"""
    c1 = TestClient(app)
    h1 = _auth(c1, "admin", "admin123")
    c2 = TestClient(app)
    h2 = _auth(c2, "writer", "writer123")
    ticket = _sealed_ticket(c1, h1)
    lock_key = f"ticket:{ticket['id']}"

    assert (
        c1.post("/api/locks/acquire", headers=h1, json={"lock_key": lock_key}).status_code
        == 200
    )
    try:
        res = c2.post(
            f"/api/tickets/{ticket['id']}/axles",
            headers=h2,
            json={"axle_weights": NEW_AXLES},
        )
        assert res.status_code == 409, (
            f"争用失败：后到会话仍受理同票改轴（HTTP {res.status_code}，{res.text}）"
        )
        assert "争用" in res.json()["detail"]
        cont = c1.get("/api/contention", headers=h1).json()["items"]
        assert any(
            x["lock_key"] == lock_key and x["actor"] == "writer" for x in cont
        ), "后到未记争用痕"
        after = c1.get(f"/api/tickets/{ticket['id']}", headers=h1).json()["ticket"]
        assert after["gross_t"] == ticket["gross_t"], "争用路径仍写入了正式轴重"
    finally:
        c1.post("/api/locks/release", headers=h1, json={"lock_key": lock_key})

    # 锁释放后，封后禁改仍然生效（拦截理由从争用变为封后）
    res2 = c2.post(
        f"/api/tickets/{ticket['id']}/axles",
        headers=h2,
        json={"axle_weights": NEW_AXLES},
    )
    assert res2.status_code == 409
    assert "封后禁改" in res2.json()["detail"]


# ---------------------------------------------------------------- 五页同态
def test_five_surfaces_homomorphic_on_sealed_state():
    """地磅页 / 对账页 / 血缘页 / 过磅页 / 快照页对已封票呈同一态。"""
    from validators import AXLE_SEAL_MINUTE_DIGEST, AXLE_SEAL_POLICY

    assert AXLE_SEAL_POLICY == "甲", "修复纪要封死为甲路径，禁止折中并存"
    assert len(AXLE_SEAL_MINUTE_DIGEST) == 64

    c = TestClient(app)
    h = _auth(c)
    ticket = _sealed_ticket(c, h)

    # 过磅页：轴重锁定、闸代可见
    assert ticket["axle_locked"] is True
    assert ticket["seal_generation"] == 2

    # 快照页：快照毛重与正式毛重同态
    seals = c.get("/api/seals", headers=h).json()["items"]
    seal = next(s for s in seals if s["seal_ref"] == SEALED_SEAL_REF)
    assert seal["snapshot_match"] is True
    assert seal["manifest_gross"] == float(ticket["gross_t"])

    # 地磅页：该桥已封闸计数与轴重锁定计数一致
    lanes = c.get("/api/lanes", headers=h).json()["items"]
    lane = next(l for l in lanes if l["id"] == ticket["lane_id"])
    bridge = c.get(f"/api/bridges/{lane['bridge_id']}", headers=h).json()
    assert bridge["sealed_tickets"] >= 1
    assert bridge["axle_locked_tickets"] == bridge["sealed_tickets"]

    # 对账页：快照同态为真，且该票毛重以正式口径入账
    recon = c.get(f"/api/reconcile/{ticket['bl_ref']}", headers=h).json()
    assert recon["snapshot_match"] is True
    row = next(t for t in recon["tickets"] if t["ticket_code"] == SEALED_CODE)
    assert float(row["gross_t"]) == float(ticket["gross_t"])
    assert recon["balanced"] is (recon["ticket_sum_t"] == recon["declared_gross_t"] or recon["balanced"])

    # 血缘页：闸代生效边可见，且无封后修订边
    edges = c.get("/api/lineage", headers=h).json()["items"]
    seal_edges = [
        e
        for e in edges
        if e["from_ticket_id"] == ticket["id"] and e["edge_type"] == "闸代生效"
    ]
    assert seal_edges, "血缘页缺闸代生效边"
    assert "闸代=2" in seal_edges[0]["detail"]
    assert seal_edges[0]["from_seal_generation"] == 2
    assert not [
        e
        for e in edges
        if e["from_ticket_id"] == ticket["id"] and e["edge_type"] == "轴重修订"
    ]


# ---------------------------------------------------------------- 封后新票可封
def test_seal_records_effective_lineage_edge():
    """封闸服务联动血缘：新建票封闸后写入闸代生效边，且封后回写被拒。"""
    c = TestClient(app)
    h = _auth(c)
    lanes = c.get("/api/lanes", headers=h).json()["items"]
    lane = next(x for x in lanes if x["lane_code"] == "LN-S-01-OUT")
    created = c.post(
        "/api/tickets",
        headers=h,
        json={
            "lane_id": lane["id"],
            "bl_ref": "BL-2026-0102",
            "vehicle_plate": "测C·00003",
            "axle_weights": [{"axle": 1, "w": 6.0}, {"axle": 2, "w": 5.5}],
            "ticket_code": "WT-AXLE-LOCK-001",
        },
    ).json()
    assert created["status"] == "已裁定"
    sealed = c.post("/api/seals", headers=h, json={"ticket_id": created["id"]})
    assert sealed.status_code == 200
    assert sealed.json()["generation"] == 2

    edges = c.get(f"/api/lineage/ticket/{created['id']}", headers=h).json()["outbound"]
    assert any(e["edge_type"] == "闸代生效" for e in edges)

    res = c.post(
        f"/api/tickets/{created['id']}/axles",
        headers=h,
        json={"axle_weights": NEW_AXLES},
    )
    assert res.status_code == 409
    assert "封后禁改" in res.json()["detail"]


# ---------------------------------------------------------------- 错位快照不得判绿
def test_stale_mismatched_snapshot_never_green():
    """历史错位快照（不经写入口、直接落库）也不得让对账判绿——禁止只换快照页显示。"""
    from db import connect

    c = TestClient(app)
    h = _auth(c)
    ticket = _sealed_ticket(c, h)

    with connect() as conn:
        conn.execute(
            "UPDATE gate_seals SET manifest=? WHERE seal_ref=?",
            (
                "票号=%s;提单=%s;车牌=%s;毛重=99.0t;闸代=2"
                % (ticket["ticket_code"], ticket["bl_ref"], ticket["vehicle_plate"]),
                SEALED_SEAL_REF,
            ),
        )
        conn.commit()

    recon = c.get(f"/api/reconcile/{ticket['bl_ref']}", headers=h).json()
    assert recon["snapshot_match"] is False
    assert recon["proof_ready"] is False, "快照错位仍判可出证明"

    proof = c.get(f"/api/proof/ticket/{SEALED_CODE}", headers=h).json()
    assert proof["snapshot_match"] is False
    assert proof["coherent"] is False

    seal = c.get(f"/api/seals/{SEALED_SEAL_REF}", headers=h).json()
    assert seal["snapshot_match"] is False
    assert seal["manifest_gross"] == 99.0


# ---------------------------------------------------------------- 封后票态不得漂移
def test_sealed_ticket_state_cannot_drift():
    """封后禁改也覆盖票态漂移：不得重开超重挂起，也不得回退成已裁定。"""
    c = TestClient(app)
    h = _auth(c)
    ticket = _sealed_ticket(c, h)

    res = c.post(
        "/api/overloads/open",
        headers=h,
        json={"ticket_id": ticket["id"], "threshold_t": 10.0},
    )
    assert res.status_code == 409
    assert "封后禁改" in res.json()["detail"]

    after = c.get(f"/api/tickets/{ticket['id']}", headers=h).json()["ticket"]
    assert after["status"] == "已封闸"
    assert after["gross_t"] == ticket["gross_t"]
