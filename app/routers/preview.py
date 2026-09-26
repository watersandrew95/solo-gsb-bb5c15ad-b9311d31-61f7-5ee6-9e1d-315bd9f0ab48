"""照明调整试算接口：不改动已存区间，仅在内存中对比现状与调整后的年度曝光。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from ..budget import build_lighting_preview, load_case_intervals
from ..database import get_db
from ..schemas import LightingPreviewIn, LightingPreviewOut
from ..timeline import parse_utc

router = APIRouter(prefix="/api/v1", tags=["lighting-preview"])


def _require_showcase(conn: sqlite3.Connection, showcase_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, name FROM showcases WHERE id = ?", (showcase_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"展柜 {showcase_id} 不存在")
    return row


@router.post(
    "/showcases/{showcase_id}/lighting-preview",
    response_model=LightingPreviewOut,
    summary="照明调整试算：临时撤去 / 新增区间，对比现状与调整后的年度曝光",
)
def lighting_preview(
    showcase_id: int, body: LightingPreviewIn, conn=Depends(get_db)
):
    showcase = _require_showcase(conn, showcase_id)
    year = body.day.year
    query_dt = datetime(body.day.year, body.day.month, body.day.day, tzinfo=timezone.utc)

    # 撤去项必须是该展柜的既有区间，否则 404
    remove_ids = list(dict.fromkeys(body.remove_interval_ids))
    found: dict[int, None] = {}
    if remove_ids:
        placeholders = ",".join("?" for _ in remove_ids)
        rows = conn.execute(
            f"SELECT id FROM light_intervals WHERE showcase_id = ? AND id IN ({placeholders})",
            (showcase_id, *remove_ids),
        ).fetchall()
        found = {int(r["id"]): None for r in rows}
    missing = [i for i in remove_ids if i not in found]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"展柜 {showcase_id} 下不存在照明区间：{missing}",
        )

    exhibits = conn.execute(
        "SELECT * FROM exhibits WHERE showcase_id = ? ORDER BY id", (showcase_id,)
    ).fetchall()

    intervals = load_case_intervals(conn, showcase_id, year)
    add_intervals = [
        (parse_utc(iv.start), parse_utc(iv.end), float(iv.lux))
        for iv in body.add_intervals
    ]
    results, removed_ids = build_lighting_preview(
        exhibits, intervals, set(remove_ids), add_intervals, year
    )

    return {
        "showcase_id": showcase["id"],
        "showcase_name": showcase["name"],
        "query_date": query_dt,
        "year": year,
        "removed_interval_ids": removed_ids,
        "added_intervals": [
            {
                "id": 0,  # 回显占位，未入库
                "showcase_id": showcase_id,
                "start": iv.start,
                "end": iv.end,
                "lux": iv.lux,
            }
            for iv in body.add_intervals
        ],
        "exhibits": results,
    }
