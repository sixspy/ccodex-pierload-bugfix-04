"""过磅与闸口校验。"""
from __future__ import annotations

import hashlib

from fastapi import HTTPException

# ---- 轴改修复第19号：修复纪要当班封死（甲路径，两路不得并存）----------------
AXLE_SEAL_POLICY = "甲"
AXLE_SEAL_MINUTE = (
    "封闸代已生效则轴重正式零改并拒写；封后不得以影子轴重草案代偿；"
    "甲路径与乙路径禁止折中并存；偏离本纪要即记封后修复偏航拒写。"
)
AXLE_SEAL_MINUTE_DIGEST = hashlib.sha256(
    AXLE_SEAL_MINUTE.encode("utf-8")
).hexdigest()

SEALED_LINEAGE_EDGE = "闸代生效"
AXLE_REVISION_EDGE_TYPES = ("轴重修订", "封后修订")
AXLE_SEAL_BLOCK_DETAIL = "封后禁改：闸代已生效，轴重正式零改并拒写"
SNAPSHOT_GROSS_TOLERANCE = 0.005


def validate_lane_open(lane: dict) -> None:
    if lane["status"] not in ("开放", "维护"):
        raise HTTPException(409, "车道已关闭")
    if lane["status"] == "维护":
        raise HTTPException(409, "车道维护中禁止过磅")


def validate_axle_count(axles: list, axle_limit: int) -> None:
    if len(axles) < 2:
        raise HTTPException(422, "至少两根轴重")
    if len(axles) > axle_limit:
        raise HTTPException(422, f"轴数超限（限 {axle_limit} 轴）")


def validate_axle_weights(axles: list) -> tuple[float, float, float]:
    weights = []
    for a in axles:
        w = float(a.get("w", a.w if hasattr(a, "w") else 0))
        if w <= 0:
            raise HTTPException(422, "轴重须为正")
        weights.append(w)
    gross = round(sum(weights), 3)
    peak_axle = max(weights)
    spread = peak_axle - min(weights)
    return gross, peak_axle, spread


def validate_gross_cap(gross: float, max_gross: float) -> None:
    if gross > max_gross * 1.15:
        raise HTTPException(422, "毛重远超地磅额定量程")
    if gross <= 0:
        raise HTTPException(422, "毛重无效")


def validate_bl_match(declared: float, gross: float, tolerance: float = 0.08) -> bool:
    if declared <= 0:
        return False
    return abs(gross - declared) / declared <= tolerance


def adjudicate_overload(gross: float, threshold: float) -> tuple[bool, float]:
    overweight = round(gross - threshold, 3)
    return overweight > 0, max(overweight, 0.0)


def validate_ticket_mutable(ticket: dict) -> None:
    if ticket["status"] in ("已放行", "作废"):
        raise HTTPException(409, "票态已锁定")


def validate_ticket_sealable(ticket: dict) -> None:
    if ticket["status"] in ("已封闸", "作废"):
        raise HTTPException(409, "票态已锁定")


def _active_seal(conn, ticket_id: int, generation: int):
    """当前闸代对应的已封闸快照（闸代生效标记的唯一来源）。"""
    return conn.execute(
        """SELECT * FROM gate_seals
           WHERE ticket_id=? AND ticket_generation=? AND status='已封闸'
           ORDER BY id DESC LIMIT 1""",
        (ticket_id, generation),
    ).fetchone()


def is_axle_locked(conn, ticket: dict) -> bool:
    """轴重是否已被闸代生效锁死（票态已封闸，或存在当前闸代的已封闸快照）。"""
    if ticket.get("status") == "已封闸":
        return True
    row = _active_seal(conn, int(ticket["id"]), int(ticket["generation"]))
    return row is not None


def validate_axle_writable(conn, ticket: dict) -> None:
    """过磅更新口必须读闸代生效标记；生效即正式轴重零改并拒写。"""
    if is_axle_locked(conn, ticket):
        raise HTTPException(409, AXLE_SEAL_BLOCK_DETAIL)


def parse_manifest_gross(manifest: str | None) -> float | None:
    for part in (manifest or "").split(";"):
        part = part.strip()
        if part.startswith("毛重=") and part.endswith("t"):
            try:
                return float(part[3:-1])
            except ValueError:
                return None
    return None


def manifest_matches_gross(manifest: str | None, gross: float) -> bool:
    snap = parse_manifest_gross(manifest)
    return snap is not None and abs(snap - float(gross)) <= SNAPSHOT_GROSS_TOLERANCE


def snapshot_gross(conn, ticket_id: int, generation: int) -> float | None:
    row = _active_seal(conn, ticket_id, generation)
    if not row:
        return None
    return parse_manifest_gross(row["manifest"])


def snapshot_coherent(conn, ticket: dict) -> bool | None:
    """快照毛重与正式毛重是否同态；无生效快照返回 None。"""
    snap = snapshot_gross(conn, int(ticket["id"]), int(ticket["generation"]))
    if snap is None:
        return None
    return abs(snap - float(ticket["gross_t"])) <= SNAPSHOT_GROSS_TOLERANCE


def sealed_bridge_stats(conn, bridge_id: int) -> tuple[int, int]:
    """（已封闸票数，其中轴重已锁数）：地磅页与快照页同态的依据。"""
    rows = conn.execute(
        """SELECT t.id, t.status, t.generation FROM weigh_tickets t
           JOIN weigh_lanes l ON l.id=t.lane_id
           WHERE l.bridge_id=? AND t.status='已封闸'""",
        (bridge_id,),
    ).fetchall()
    sealed = len(rows)
    locked = sum(1 for r in rows if is_axle_locked(conn, dict(r)))
    return sealed, locked


def validate_no_open_overload(conn, ticket_id: int) -> None:
    row = conn.execute(
        """SELECT id FROM overload_holds
           WHERE ticket_id=? AND status='开放' LIMIT 1""",
        (ticket_id,),
    ).fetchone()
    if row:
        raise HTTPException(409, "存在开放超重挂起，不得放行")


def format_manifest(ticket: dict, gross: float, generation: int) -> str:
    return (
        f"票号={ticket['ticket_code']};"
        f"提单={ticket['bl_ref']};"
        f"车牌={ticket['vehicle_plate']};"
        f"毛重={gross}t;"
        f"闸代={generation}"
    )