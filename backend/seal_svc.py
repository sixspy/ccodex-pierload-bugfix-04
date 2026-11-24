"""闸代快照：升代并写清单。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import release, require_lock
from models import SealIn
from validators import (
    SEALED_LINEAGE_EDGE,
    format_manifest,
    is_axle_locked,
    manifest_matches_gross,
    parse_manifest_gross,
    validate_ticket_sealable,
)

router = APIRouter(tags=["seal"])


@router.get("/api/seals")
def list_seals(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT s.*, t.ticket_code, t.vehicle_plate, t.gross_t,
                      t.generation AS current_generation, t.status AS ticket_status
               FROM gate_seals s
               JOIN weigh_tickets t ON t.id=s.ticket_id ORDER BY s.id DESC"""
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            _decorate_seal_row(d)
            items.append(d)
        return {"items": items}


def _decorate_seal_row(d: dict) -> None:
    """快照页/血缘页同态：清单毛重与正式毛重对齐，闸代与当前代一致即生效。"""
    d["manifest_gross"] = parse_manifest_gross(d["manifest"])
    d["snapshot_match"] = manifest_matches_gross(d["manifest"], float(d["gross_t"]))
    d["axle_locked"] = (
        d["ticket_status"] == "已封闸"
        or _seal_generation_effective(d)
    )


def _seal_generation_effective(d: dict) -> bool:
    return (
        d.get("status") == "已封闸"
        and int(d.get("ticket_generation", -1)) == int(d.get("current_generation", -2))
    )


@router.post("/api/seals")
def create_seal(body: SealIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    lock_key = f"ticket:{body.ticket_id}"
    with connect() as conn:
        require_lock(conn, lock_key, user)
        try:
            ticket = conn.execute(
                "SELECT * FROM weigh_tickets WHERE id=?", (body.ticket_id,)
            ).fetchone()
            if not ticket:
                raise HTTPException(404, "过磅票不存在")
            validate_ticket_sealable(dict(ticket))
            if ticket["status"] == "旁路挂起":
                raise HTTPException(409, "旁路挂起不得封闸")
            open_hold = conn.execute(
                """SELECT id FROM overload_holds WHERE ticket_id=? AND status='开放'""",
                (body.ticket_id,),
            ).fetchone()
            if open_hold:
                raise HTTPException(409, "超重开放不得封闸")
            next_gen = int(ticket["generation"]) + 1
            manifest = body.manifest or format_manifest(
                dict(ticket), float(ticket["gross_t"]), next_gen
            )
            if not manifest_matches_gross(manifest, float(ticket["gross_t"])):
                raise HTTPException(
                    422, "清单毛重与正式毛重不一致，封闸不得制造错位快照"
                )
            now = now_iso()
            seal_ref = f"GS-{ticket['ticket_code']}-G{next_gen}"
            cur = conn.execute(
                """INSERT INTO gate_seals(ticket_id,ticket_generation,manifest,seal_ref,status,sealed_at)
                   VALUES (?,?,?,?,?,?)""",
                (body.ticket_id, next_gen, manifest, seal_ref, "已封闸", now),
            )
            conn.execute(
                """UPDATE weigh_tickets SET generation=?, status='已封闸' WHERE id=?""",
                (next_gen, body.ticket_id,),
            )
            conn.execute(
                """INSERT INTO axle_lineage(from_ticket_id,to_ticket_id,edge_type,detail,created_at)
                   VALUES (?,?,?,?,?)""",
                (
                    body.ticket_id,
                    None,
                    SEALED_LINEAGE_EDGE,
                    f"票={ticket['ticket_code']};闸代={next_gen};快照={seal_ref};毛重={float(ticket['gross_t'])}t",
                    now,
                ),
            )
            write_audit(
                conn,
                user,
                "gate_seal",
                seal_ref,
                {
                    "seal_id": cur.lastrowid,
                    "generation": next_gen,
                    "manifest": manifest,
                },
            )
            conn.commit()
            return {
                "id": cur.lastrowid,
                "seal_ref": seal_ref,
                "generation": next_gen,
                "manifest": manifest,
                "status": "已封闸",
            }
        finally:
            release(conn, lock_key, user)


@router.get("/api/seals/{seal_ref}")
def get_seal(seal_ref: str, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            """SELECT s.*, t.ticket_code, t.axle_weights, t.gross_t, t.generation,
                      t.status AS ticket_status
               FROM gate_seals s JOIN weigh_tickets t ON t.id=s.ticket_id
               WHERE s.seal_ref=?""",
            (seal_ref,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "快照不存在")
        data = dict(row)
        data["axle_weights"] = json.loads(data["axle_weights"] or "[]")
        data["manifest_gross"] = parse_manifest_gross(data["manifest"])
        data["snapshot_match"] = manifest_matches_gross(
            data["manifest"], float(data["gross_t"])
        )
        data["current_generation"] = int(data["generation"])
        # 注意：s.* 的 id 是快照主键，闸代判定须按票主键与票态取
        data["axle_locked"] = is_axle_locked(
            conn,
            {
                "id": data["ticket_id"],
                "generation": data["generation"],
                "status": data["ticket_status"],
            },
        )
        return data