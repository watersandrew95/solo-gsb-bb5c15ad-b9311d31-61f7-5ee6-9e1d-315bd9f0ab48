"""展柜、展品与照明区间接口。"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from ..budget import resolve_limit
from ..database import get_db
from ..schemas import (
    ExhibitCreate,
    ExhibitOut,
    ExhibitUpdate,
    LightIntervalBulk,
    LightIntervalOut,
    ShowcaseCreate,
    ShowcaseOut,
)
from ..timeline import day_bounds, fmt_utc, parse_utc

router = APIRouter(prefix="/api/v1", tags=["showcases"])


def _get_showcase(conn: sqlite3.Connection, showcase_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, name, location FROM showcases WHERE id = ?", (showcase_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"展柜 {showcase_id} 不存在")
    return row


def _get_exhibit(conn: sqlite3.Connection, exhibit_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM exhibits WHERE id = ?", (exhibit_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"展品 {exhibit_id} 不存在")
    return row


def _serialize_interval(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "showcase_id": r["showcase_id"],
        "start": parse_utc(r["start_ts"]),
        "end": parse_utc(r["end_ts"]),
        "lux": r["lux"],
    }


# ------------------------------------------------------------------ 展柜

@router.post("/showcases", response_model=ShowcaseOut, status_code=201)
def create_showcase(body: ShowcaseCreate, conn=Depends(get_db)):
    try:
        cur = conn.execute(
            "INSERT INTO showcases (name, location) VALUES (?, ?)",
            (body.name, body.location),
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail=f"展柜名称已存在：{body.name}") from exc
    return _get_showcase(conn, cur.lastrowid)


@router.get("/showcases", response_model=list[ShowcaseOut])
def list_showcases(conn=Depends(get_db)):
    return conn.execute("SELECT id, name, location FROM showcases ORDER BY id").fetchall()


@router.get("/showcases/{showcase_id}", response_model=ShowcaseOut)
def get_showcase(showcase_id: int, conn=Depends(get_db)):
    return _get_showcase(conn, showcase_id)


# ------------------------------------------------------------------ 展品

@router.post(
    "/showcases/{showcase_id}/exhibits",
    response_model=ExhibitOut,
    status_code=201,
)
def create_exhibit(showcase_id: int, body: ExhibitCreate, conn=Depends(get_db)):
    _get_showcase(conn, showcase_id)
    limit = resolve_limit(body.sensitivity, body.annual_limit_lux_hours)
    try:
        cur = conn.execute(
            """
            INSERT INTO exhibits (showcase_id, name, sensitivity, annual_limit_lux_hours)
            VALUES (?, ?, ?, ?)
            """,
            (showcase_id, body.name, body.sensitivity.value, limit),
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"展柜 {showcase_id} 下已存在同名展品：{body.name}",
        ) from exc
    return _get_exhibit(conn, cur.lastrowid)


@router.get("/showcases/{showcase_id}/exhibits", response_model=list[ExhibitOut])
def list_exhibits(showcase_id: int, conn=Depends(get_db)):
    _get_showcase(conn, showcase_id)
    return conn.execute(
        "SELECT * FROM exhibits WHERE showcase_id = ? ORDER BY id", (showcase_id,)
    ).fetchall()


@router.get("/exhibits/{exhibit_id}", response_model=ExhibitOut)
def get_exhibit(exhibit_id: int, conn=Depends(get_db)):
    return _get_exhibit(conn, exhibit_id)


@router.patch("/exhibits/{exhibit_id}", response_model=ExhibitOut)
def move_exhibit(exhibit_id: int, body: ExhibitUpdate, conn=Depends(get_db)):
    _get_exhibit(conn, exhibit_id)
    _get_showcase(conn, body.showcase_id)  # 拒绝未知目标展柜
    conn.execute(
        "UPDATE exhibits SET showcase_id = ? WHERE id = ?",
        (body.showcase_id, exhibit_id),
    )
    return _get_exhibit(conn, exhibit_id)


# --------------------------------------------------------------- 照明区间

def _insert_interval(conn, showcase_id: int, interval) -> int:
    cur = conn.execute(
        """
        INSERT INTO light_intervals (showcase_id, start_ts, end_ts, lux)
        VALUES (?, ?, ?, ?)
        """,
        (showcase_id, fmt_utc(interval.start), fmt_utc(interval.end), interval.lux),
    )
    return cur.lastrowid


@router.post(
    "/showcases/{showcase_id}/intervals",
    response_model=list[LightIntervalOut],
    status_code=201,
)
def add_intervals(showcase_id: int, body: LightIntervalBulk, conn=Depends(get_db)):
    _get_showcase(conn, showcase_id)
    ids = [_insert_interval(conn, showcase_id, iv) for iv in body.intervals]
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT id, showcase_id, start_ts, end_ts, lux
        FROM light_intervals WHERE id IN ({placeholders}) ORDER BY id
        """,
        ids,
    ).fetchall()
    return [_serialize_interval(r) for r in rows]


@router.get("/showcases/{showcase_id}/intervals", response_model=list[LightIntervalOut])
def list_intervals(
    showcase_id: int,
    day: date | None = Query(default=None, description="仅返回与该日期（UTC）有交集的区间"),
    conn=Depends(get_db),
):
    _get_showcase(conn, showcase_id)
    if day is None:
        rows = conn.execute(
            """
            SELECT id, showcase_id, start_ts, end_ts, lux
            FROM light_intervals WHERE showcase_id = ? ORDER BY start_ts
            """,
            (showcase_id,),
        ).fetchall()
    else:
        day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        _, day_end = day_bounds(day_start)
        rows = conn.execute(
            """
            SELECT id, showcase_id, start_ts, end_ts, lux
            FROM light_intervals
            WHERE showcase_id = ? AND end_ts > ? AND start_ts < ?
            ORDER BY start_ts
            """,
            (showcase_id, day_start.isoformat(), day_end.isoformat()),
        ).fetchall()

    return [_serialize_interval(r) for r in rows]
