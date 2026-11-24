"""轴重血缘边。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from models import LineageIn
from validators import (
    AXLE_REVISION_EDGE_TYPES,
    SEALED_LINEAGE_EDGE,
    is_axle_locked,
    parse_manifest_gross,
)

router = APIRouter(tags=["lineage"])


def _decorate_edge(conn, edge: dict) -> dict:
    """闸代生效边回显所属闸代与快照毛重；供血缘页与快照页同态核对。"""
    if edge.get("edge_type") != SEALED_LINEAGE_EDGE:
        return edge
    seal = conn.execute(
        """SELECT * FROM gate_seals WHERE ticket_id=? AND status='已封闸'
           ORDER BY id DESC LIMIT 1""",
        (edge["from_ticket_id"],),
    ).fetchone()
    if seal:
        edge["from_seal_generation"] = int(seal["ticket_generation"])
        edge["from_seal_ref"] = seal["seal_ref"]
        edge["from_manifest_gross"] = parse_manifest_gross(seal["manifest"])
    return edge


@router.get("/api/lineage")
def list_lineage(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT e.*, a.ticket_code from_code, b.ticket_code to_code
               FROM axle_lineage e
               JOIN weigh_tickets a ON a.id=e.from_ticket_id
               LEFT JOIN weigh_tickets b ON b.id=e.to_ticket_id
               ORDER BY e.id DESC"""
        ).fetchall()
        return {"items": [_decorate_edge(conn, dict(r)) for r in rows]}


@router.get("/api/lineage/ticket/{ticket_id}")
def lineage_for_ticket(ticket_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        out_edges = conn.execute(
            """SELECT e.*, b.ticket_code to_code FROM axle_lineage e
               LEFT JOIN weigh_tickets b ON b.id=e.to_ticket_id
               WHERE e.from_ticket_id=?""",
            (ticket_id,),
        ).fetchall()
        in_edges = conn.execute(
            """SELECT e.*, a.ticket_code from_code FROM axle_lineage e
               JOIN weigh_tickets a ON a.id=e.from_ticket_id
               WHERE e.to_ticket_id=?""",
            (ticket_id,),
        ).fetchall()
        return {
            "outbound": [_decorate_edge(conn, dict(x)) for x in out_edges],
            "inbound": [_decorate_edge(conn, dict(x)) for x in in_edges],
        }


@router.post("/api/lineage")
def add_lineage(body: LineageIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        fr = conn.execute(
            "SELECT id, ticket_code FROM weigh_tickets WHERE id=?",
            (body.from_ticket_id,),
        ).fetchone()
        if not fr:
            raise HTTPException(404, "起点票不存在")
        if body.to_ticket_id:
            to = conn.execute(
                "SELECT id FROM weigh_tickets WHERE id=?", (body.to_ticket_id,)
            ).fetchone()
            if not to:
                raise HTTPException(404, "终点票不存在")
        if body.edge_type in AXLE_REVISION_EDGE_TYPES:
            fr_ticket = conn.execute(
                "SELECT * FROM weigh_tickets WHERE id=?", (body.from_ticket_id,)
            ).fetchone()
            if is_axle_locked(conn, dict(fr_ticket)):
                raise HTTPException(
                    409,
                    "封后禁改：闸代已生效，不得登记轴重修订边（影子草案不得混入血缘）",
                )
        now = now_iso()
        cur = conn.execute(
            """INSERT INTO axle_lineage(from_ticket_id,to_ticket_id,edge_type,detail,created_at)
               VALUES (?,?,?,?,?)""",
            (
                body.from_ticket_id,
                body.to_ticket_id,
                body.edge_type,
                body.detail,
                now,
            ),
        )
        write_audit(
            conn,
            user,
            "lineage_edge",
            fr["ticket_code"],
            {"edge_id": cur.lastrowid, "edge_type": body.edge_type},
        )
        conn.commit()
        return {"id": cur.lastrowid}