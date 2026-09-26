"""API 集成测试：每个测试函数使用独立的临时数据库。"""
from __future__ import annotations

import importlib
import os
import tempfile
from datetime import datetime, timezone

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_file))

    import app.config
    import app.database
    import app.main
    importlib.reload(app.config)
    importlib.reload(app.database)
    importlib.reload(app.main)

    from fastapi.testclient import TestClient

    with TestClient(app.main.app) as c:
        yield c


def _create_case(client, name="丝绸展柜", location="一号厅"):
    r = client.post("/api/v1/showcases", json={"name": name, "location": location})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_health_and_root(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"
    assert client.get("/").json()["docs"] == "/docs"


def test_showcase_and_exhibit_crud(client):
    case_id = _create_case(client)
    r = client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "宋代丝帛", "sensitivity": "high"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["annual_limit_lux_hours"] == 15000  # high 默认年度上限
    exhibit_id = body["id"]

    assert client.get(f"/api/v1/exhibits/{exhibit_id}").status_code == 200
    # 未知展品 404
    assert client.get("/api/v1/exhibits/9999").status_code == 404
    # 未知展柜上挂展品 404
    r = client.post(
        "/api/v1/showcases/9999/exhibits",
        json={"name": "x", "sensitivity": "low"},
    )
    assert r.status_code == 404
    # 重名展柜 409
    assert client.post("/api/v1/showcases", json={"name": "丝绸展柜"}).status_code == 409


def test_custom_annual_limit_and_low_sensitivity_unlimited(client):
    case_id = _create_case(client)
    r = client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "定制", "sensitivity": "medium", "annual_limit_lux_hours": 1000},
    )
    assert r.json()["annual_limit_lux_hours"] == 1000

    r = client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "石碑", "sensitivity": "low"},
    )
    assert r.json()["annual_limit_lux_hours"] is None


def test_reject_negative_lux(client):
    case_id = _create_case(client)
    r = client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T10:00:00Z", "lux": -5}
        ]},
    )
    assert r.status_code == 422
    assert "照度" in r.text or "greater than or equal" in r.text


def test_reject_reverse_interval(client):
    case_id = _create_case(client)
    r = client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T10:00:00Z", "end": "2026-03-01T08:00:00Z", "lux": 50}
        ]},
    )
    assert r.status_code == 422
    assert "结束时间" in r.text


def test_reject_zero_length_interval(client):
    case_id = _create_case(client)
    r = client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T10:00:00Z", "end": "2026-03-01T10:00:00Z", "lux": 50}
        ]},
    )
    assert r.status_code == 422


def test_unknown_showcase_interval_rejected(client):
    r = client.post(
        "/api/v1/showcases/77/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T10:00:00Z", "lux": 50}
        ]},
    )
    assert r.status_code == 404


def test_overlap_budget_max_lux_and_first_exceed(client):
    """端到端：重叠灯光取 max；高敏展品上限 1000 lux·h。

    区间 A: 08:00-18:00 50 lux（背景灯）
    区间 B: 10:00-12:00 200 lux（重点照明，与 A 重叠 → 取 200）
    合并后曝光 = 50*2 + 200*2 + 50*6 = 800 lux·h（当天未超标）
    再加 次日 08:00-16:00 50 lux = 400 → 年度累计 1200，超标 200。
    首次超标：首日 800 之后，次日还需 200，50 lux → 4h，即次日 12:00。
    """
    case_id = _create_case(client, name="预算展柜")
    r = client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "丝画", "sensitivity": "high", "annual_limit_lux_hours": 1000},
    )
    exhibit_id = r.json()["id"]

    r = client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T18:00:00Z", "lux": 50},
            {"start": "2026-03-01T10:00:00Z", "end": "2026-03-01T12:00:00Z", "lux": 200},
            {"start": "2026-03-02T08:00:00Z", "end": "2026-03-02T16:00:00Z", "lux": 50},
        ]},
    )
    assert r.status_code == 201, r.text
    assert len(r.json()) == 3

    # 按展柜 + 日期查询（3月1日当天：800，未超标）
    r = client.get(f"/api/v1/showcases/{case_id}/budget?day=2026-03-01")
    assert r.status_code == 200, r.text
    budget = r.json()["exhibits"][0]
    assert budget["total_lux_hours"] == 1200.0
    assert budget["daily_used_lux_hours"] == 800.0
    assert budget["first_exceed_at"] is None or budget["first_exceed_at"].startswith("2026-03-02")

    # 按展品查询 3月2日：应报告首次超标时刻与超出量
    r = client.get(f"/api/v1/exhibits/{exhibit_id}/budget?day=2026-03-02")
    budget = r.json()
    assert budget["first_exceed_at"].startswith("2026-03-02T12:00:00")
    assert budget["exceeded_lux_hours"] == 200.0
    assert budget["remaining_lux_hours"] == 0.0
    assert budget["daily_used_lux_hours"] == 400.0
    # 日额度 = 1000 / 365，3月2日剩余 = 日额度 - 400
    assert budget["daily_quota_lux_hours"] == round(1000 / 365, 6)
    assert budget["daily_remaining_lux_hours"] == round(1000 / 365 - 400, 6)
    assert budget["annual_remaining_at_date"] == -200.0


def test_query_intervals_by_day(client):
    case_id = _create_case(client, name="日期过滤柜")
    client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T20:00:00Z", "end": "2026-03-02T04:00:00Z", "lux": 30},
            {"start": "2026-03-05T08:00:00Z", "end": "2026-03-05T10:00:00Z", "lux": 30},
        ]},
    )
    days = client.get(
        f"/api/v1/showcases/{case_id}/intervals?day=2026-03-02"
    ).json()
    assert len(days) == 1  # 跨夜区间与 3月2日有交集
    assert days[0]["lux"] == 30

    assert client.get(
        f"/api/v1/showcases/{case_id}/intervals?day=2026-03-04"
    ).json() == []


def test_naive_datetime_treated_as_utc(client):
    case_id = _create_case(client, name="无时区柜")
    r = client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00", "end": "2026-03-01T10:00:00", "lux": 50}
        ]},
    )
    assert r.status_code == 201
    assert r.json()[0]["start"].endswith(("+00:00", "Z"))


def test_move_exhibit_rejects_unknown_target(client):
    c1 = _create_case(client, name="源柜")
    c2 = _create_case(client, name="目标柜")
    ex = client.post(
        f"/api/v1/showcases/{c1}/exhibits",
        json={"name": "陶俑", "sensitivity": "medium"},
    ).json()
    r = client.patch(f"/api/v1/exhibits/{ex['id']}", json={"showcase_id": c2})
    assert r.status_code == 200 and r.json()["showcase_id"] == c2
    r = client.patch(f"/api/v1/exhibits/{ex['id']}", json={"showcase_id": 9999})
    assert r.status_code == 404


def test_all_budgets_and_filter(client):
    c1 = _create_case(client, name="柜A")
    c2 = _create_case(client, name="柜B")
    for cid in (c1, c2):
        client.post(
            f"/api/v1/showcases/{cid}/exhibits",
            json={"name": "铜器", "sensitivity": "low"},
        )
    r = client.get("/api/v1/budgets?day=2026-03-01")
    assert r.status_code == 200 and len(r.json()) == 2

    r = client.get(f"/api/v1/budgets?day=2026-03-01&showcase_id={c1}")
    assert len(r.json()) == 1 and r.json()[0]["showcase_id"] == c1

    assert client.get("/api/v1/budgets?showcase_id=4242").status_code == 404


def test_persistence_across_connections(client, tmp_path):
    """同一 DB_PATH 下新建客户端连接，数据仍在（验证 SQLite 落盘）。"""
    case_id = _create_case(client, name="持久化柜")
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "敦煌写经", "sensitivity": "high"},
    )

    import app.main
    from fastapi.testclient import TestClient

    with TestClient(app.main.app) as second:
        r = second.get(f"/api/v1/showcases/{case_id}/exhibits")
        assert r.status_code == 200 and r.json()[0]["name"] == "敦煌写经"


# ------------------------------------------------------------- 照明调整试算


def _add_intervals(client, case_id, intervals):
    r = client.post(
        f"/api/v1/showcases/{case_id}/intervals", json={"intervals": intervals}
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_lighting_preview_remove_and_add(client):
    """端到端试算：撤去跨年区间 + 新增重叠区间，对比现状 / 调整后年度曝光。

    既有区间（2026 年视角）：
      A 跨年 2025-12-31 20:00 → 2026-01-01 04:00 @100 → 当年计入 400
      B 03-01 08:00–12:00 @50                        → 200
      C 03-02 08:00–10:00 @100                       → 200
    现状年度合计 800；绢画（上限 500）在 B 段补 100 → 03-01 10:00 首次超限。
    试算：撤去 A、C，新增 D 03-01 09:00–11:00 @300（与 B 重叠取 max）
      合并后 08–09 @50 + 09–11 @300 + 11–12 @50 = 700，差额 -100；
      绢画需再补 450，300 lux → 1.5h，即 03-01 10:30 首次超限。
    """
    case_id = _create_case(client, name="试算柜")
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "绢画", "sensitivity": "high", "annual_limit_lux_hours": 500},
    )
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "石兽", "sensitivity": "low"},  # 不限量
    )
    created = _add_intervals(client, case_id, [
        {"start": "2025-12-31T20:00:00Z", "end": "2026-01-01T04:00:00Z", "lux": 100},
        {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T12:00:00Z", "lux": 50},
        {"start": "2026-03-02T08:00:00Z", "end": "2026-03-02T10:00:00Z", "lux": 100},
    ])
    id_a, _, id_c = created[0]["id"], created[1]["id"], created[2]["id"]

    r = client.post(
        f"/api/v1/showcases/{case_id}/lighting-preview",
        json={
            "day": "2026-03-01",
            "remove_interval_ids": [id_a, id_c],
            "add_intervals": [
                {"start": "2026-03-01T09:00:00Z", "end": "2026-03-01T11:00:00Z", "lux": 300}
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["showcase_id"] == case_id
    assert body["year"] == 2026
    assert body["query_date"].startswith("2026-03-01")
    assert body["removed_interval_ids"] == [id_a, id_c]
    assert len(body["added_intervals"]) == 1
    assert body["added_intervals"][0]["lux"] == 300

    silk, stone = body["exhibits"]
    assert silk["exhibit_name"] == "绢画"
    assert silk["current_total_lux_hours"] == 800.0
    assert silk["preview_total_lux_hours"] == 700.0
    assert silk["delta_lux_hours"] == -100.0
    assert silk["current_first_exceed_at"].startswith("2026-03-01T10:00:00")
    assert silk["preview_first_exceed_at"].startswith("2026-03-01T10:30:00")

    # 不限量展品：照常统计曝光，越限时刻为 null
    assert stone["exhibit_name"] == "石兽"
    assert stone["annual_limit_lux_hours"] is None
    assert stone["current_total_lux_hours"] == 800.0
    assert stone["preview_total_lux_hours"] == 700.0
    assert stone["delta_lux_hours"] == -100.0
    assert stone["current_first_exceed_at"] is None
    assert stone["preview_first_exceed_at"] is None

    # 试算不改动已存区间
    assert len(client.get(f"/api/v1/showcases/{case_id}/intervals").json()) == 3
    # 现有预算接口结果保持一致
    budget = client.get(
        f"/api/v1/showcases/{case_id}/budget?day=2026-03-01"
    ).json()["exhibits"][0]
    assert budget["total_lux_hours"] == 800.0
    assert budget["first_exceed_at"].startswith("2026-03-01T10:00:00")


def test_lighting_preview_unknown_interval_or_showcase_404(client):
    c1 = _create_case(client, name="试算源柜")
    c2 = _create_case(client, name="试算空柜")
    other = _add_intervals(client, c1, [
        {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T10:00:00Z", "lux": 50}
    ])[0]

    # 撤去项属于别的展柜 → 404
    r = client.post(
        f"/api/v1/showcases/{c2}/lighting-preview",
        json={"day": "2026-03-01", "remove_interval_ids": [other["id"]]},
    )
    assert r.status_code == 404
    # 不存在的区间 ID → 404
    r = client.post(
        f"/api/v1/showcases/{c1}/lighting-preview",
        json={"day": "2026-03-01", "remove_interval_ids": [other["id"], 999999]},
    )
    assert r.status_code == 404
    # 未知展柜 → 404
    r = client.post(
        "/api/v1/showcases/9999/lighting-preview",
        json={"day": "2026-03-01"},
    )
    assert r.status_code == 404
    # 空柜 / 空调整：合法请求，返回空展品列表
    r = client.post(
        f"/api/v1/showcases/{c2}/lighting-preview", json={"day": "2026-03-01"}
    )
    assert r.status_code == 200
    assert r.json()["exhibits"] == []
    assert r.json()["removed_interval_ids"] == []


def test_lighting_preview_input_validation(client):
    case_id = _create_case(client, name="试算校验柜")
    # 新增区间沿用现有照度校验：负照度 → 422
    r = client.post(
        f"/api/v1/showcases/{case_id}/lighting-preview",
        json={
            "day": "2026-03-01",
            "add_intervals": [
                {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T10:00:00Z", "lux": -5}
            ],
        },
    )
    assert r.status_code == 422
    # 反向区间 → 422
    r = client.post(
        f"/api/v1/showcases/{case_id}/lighting-preview",
        json={
            "day": "2026-03-01",
            "add_intervals": [
                {"start": "2026-03-01T10:00:00Z", "end": "2026-03-01T08:00:00Z", "lux": 50}
            ],
        },
    )
    assert r.status_code == 422
    # 缺少查询日 → 422
    r = client.post(f"/api/v1/showcases/{case_id}/lighting-preview", json={})
    assert r.status_code == 422
