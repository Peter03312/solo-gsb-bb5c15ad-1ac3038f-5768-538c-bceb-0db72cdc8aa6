"""逐分钟误差修订与误差带核验测试。"""
from app.database import connect

from conftest import add_node, add_segment, seed_initial_revision


def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _add_error_revision(client, errors, base_revision=None, note=None):
    body = {"errors": errors}
    if base_revision is not None:
        body["base_revision"] = base_revision
    if note is not None:
        body["note"] = note
    return client.post("/api/tide/error-revisions", json=body)


def _plan_confirm(client, rev, draft=1.0, height=0.0, margin=0.0,
                  start=0, window=20, origin="A", destination="B"):
    body = {
        "origin": origin, "destination": destination,
        "start_minute": start, "window_minutes": window,
        "draft": draft, "height": height, "margin": margin,
        "revision": rev,
    }
    r = client.post("/api/trips/plan", json=body)
    assert r.status_code == 200, r.text
    r = client.post("/api/trips/confirm", json=r.json())
    assert r.status_code == 201, r.text
    return r.json()


def _review(client, trip_id, target_revision=None):
    body = {} if target_revision is None else {"target_revision": target_revision}
    return client.post(f"/api/trips/{trip_id}/review", json=body)


# ---------------------------------------------------------------------------
# 1. 误差修订：仅覆盖列出分钟，未列出分钟继承，名义潮位不变，生成新修订号
# ---------------------------------------------------------------------------

def test_error_revision_overrides_only_listed_minutes(client):
    add_node(client, "A"); add_node(client, "B")
    rev1 = seed_initial_revision(_level(0, 19, 3.0))

    r = _add_error_revision(client, {5: 0.5, 10: 1.25}, base_revision=rev1)
    assert r.status_code == 201, r.text
    rev2 = r.json()["revision"]
    assert rev2 == rev1 + 1

    g = client.get(f"/api/tide/revisions/{rev2}",
                   params={"from_minute": 0, "to_minute": 19}).json()
    levels = {int(k): v for k, v in g["levels"].items()}
    errors = {int(k): v for k, v in g["errors"].items()}
    assert all(v == 3.0 for v in levels.values())       # 名义潮位不变
    assert errors[5] == 0.5
    assert errors[10] == 1.25
    assert errors[0] == 0.0                              # 未列出继承（旧值为 0）
    assert errors[19] == 0.0

    # 基础修订不可变：误差仍全部为 0
    g1 = client.get(f"/api/tide/revisions/{rev1}",
                    params={"from_minute": 0, "to_minute": 19}).json()
    assert all(v == 0.0 for v in g1["errors"].values())


def test_error_revision_default_base_is_latest_and_chains(client):
    add_node(client, "A"); add_node(client, "B")
    rev1 = seed_initial_revision(_level(0, 19, 3.0))

    r = _add_error_revision(client, {3: 0.2})            # 省略 base -> 最新
    rev2 = r.json()["revision"]
    r = _add_error_revision(client, {4: 0.7})            # 再以 rev2 为基础
    rev3 = r.json()["revision"]
    assert (rev2, rev3) == (rev1 + 1, rev1 + 2)

    g = client.get(f"/api/tide/revisions/{rev3}",
                   params={"from_minute": 0, "to_minute": 19}).json()
    errors = {int(k): v for k, v in g["errors"].items()}
    assert errors[3] == 0.2                               # 继承自 rev2
    assert errors[4] == 0.7                               # 本次覆盖
    assert errors[5] == 0.0


# ---------------------------------------------------------------------------
# 2. 旧修订视为误差 0（存量库迁移）
# ---------------------------------------------------------------------------

def test_legacy_revisions_default_error_zero(tmp_path, monkeypatch):
    # 直接按旧结构建库（无 error 列）并写入修订，验证启动迁移后旧修订误差为 0
    import sqlite3
    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE tide_revisions (revision INTEGER PRIMARY KEY"
                 " AUTOINCREMENT, created_at TEXT, note TEXT)")
    conn.execute("CREATE TABLE tide_values (revision INTEGER, minute INTEGER,"
                 " level REAL NOT NULL, PRIMARY KEY (revision, minute))")
    conn.execute("INSERT INTO tide_revisions(revision, note) VALUES (1, 'legacy')")
    conn.executemany("INSERT INTO tide_values(revision, minute, level) VALUES (1, ?, 3.0)",
                     [(m,) for m in range(20)])
    conn.commit()
    conn.close()

    monkeypatch.setenv("DB_PATH", str(db_file))
    from app import config
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    from app.database import connect as app_connect, init_db
    c = app_connect()
    init_db(c)
    cols = {r[1] for r in c.execute("PRAGMA table_info(tide_values)").fetchall()}
    assert "error" in cols
    errs = [r[0] for r in c.execute(
        "SELECT DISTINCT error FROM tide_values WHERE revision = 1").fetchall()]
    assert errs == [0.0]
    c.close()


# ---------------------------------------------------------------------------
# 3. 名义潮位更正继承误差；未改分钟名义潮位不变
# ---------------------------------------------------------------------------

def test_nominal_correction_inherits_errors_and_keeps_others(client):
    add_node(client, "A"); add_node(client, "B")
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    _add_error_revision(client, {2: 0.4, 7: 0.9})

    # 名义更正只改第 7 分钟
    r = client.post("/api/tide/corrections",
                    json={"corrections": {7: 1.0}, "note": "第7分钟低潮更正"})
    assert r.status_code == 201, r.text
    rev3 = r.json()["revision"]
    g = client.get(f"/api/tide/revisions/{rev3}",
                   params={"from_minute": 0, "to_minute": 19}).json()
    levels = {int(k): v for k, v in g["levels"].items()}
    errors = {int(k): v for k, v in g["errors"].items()}
    assert levels[7] == 1.0
    assert levels[2] == 3.0                               # 未改分钟名义潮位不变
    assert errors[2] == 0.4                               # 未改分钟误差继承
    assert errors[7] == 0.0                               # 被改分钟误差随名义更正归零
    assert errors[8] == 0.0


# ---------------------------------------------------------------------------
# 4. 规划/确认：水深按 名义−误差、净空按 名义+误差 核验；等待不校验
# ---------------------------------------------------------------------------

def test_planning_depth_uses_level_minus_error(client):
    add_node(client, "A"); add_node(client, "B")
    # 深 4m，吃水 6.0 + 余量 0.5 => 名义−误差 >= 2.5
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))

    # 误差 0.6：4 + 3 - 0.6 = 6.4 < 6.5 => 分钟 0 不可行；分钟 1+ 无误差可行
    rev2 = _add_error_revision(client, {0: 0.6}, base_revision=rev1).json()["revision"]
    r = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 6.0, "height": 0.0, "margin": 0.5,
        "revision": rev2,
    })
    assert r.status_code == 200, r.text
    leg = r.json()["itinerary"][0]
    assert leg["start_minute"] == 1                        # 等待第 0 分钟（不校验）
    assert leg["end_minute"] == 3

    # 误差 0.4：4 + 3 - 0.4 = 6.6 >= 6.5 => 第 0 分钟可行
    rev3 = _add_error_revision(client, {0: 0.4}, base_revision=rev1).json()["revision"]
    r = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 6.0, "height": 0.0, "margin": 0.5,
        "revision": rev3,
    })
    assert r.json()["itinerary"][0]["start_minute"] == 0


def test_planning_clearance_uses_level_plus_error(client):
    add_node(client, "A"); add_node(client, "B")
    # 净空 5m，船高 3.0 + 余量 1.0 => 5 - (名义+误差) >= 4，即 名义+误差 <= 1
    add_segment(client, "A", "B", travel_time=1, depth=99.0, clearance=5.0)
    rev1 = seed_initial_revision(_level(0, 19, 0.0))

    # 全窗口名义 0 + 误差 1.5 => 高潮位 1.5，5-1.5=3.5 < 4 全程不可行
    rev2 = _add_error_revision(
        client, {m: 1.5 for m in range(20)}, base_revision=rev1
    ).json()["revision"]
    r = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 3.0, "margin": 1.0,
        "revision": rev2,
    })
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NO_FEASIBLE_ITINERARY"

    # 全窗口误差 1.0 => 5-1=4 恰好可行
    rev3 = _add_error_revision(
        client, {m: 1.0 for m in range(20)}, base_revision=rev1
    ).json()["revision"]
    r = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 3.0, "margin": 1.0,
        "revision": rev3,
    })
    assert r.status_code == 200
    assert r.json()["itinerary"][0]["start_minute"] == 0


def test_confirm_recomputes_with_error_band(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=1, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    rev2 = _add_error_revision(client, {0: 0.6}, base_revision=rev1).json()["revision"]

    # 以 rev2 规划须等待；篡改回第 0 分钟出发，确认复核应判 PLAN_MISMATCH
    plan = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 6.0, "height": 0.0, "margin": 0.5,
        "revision": rev2,
    }).json()
    assert plan["itinerary"][0]["start_minute"] == 1
    plan["itinerary"][0]["start_minute"] = 0
    r = client.post("/api/trips/confirm", json=plan)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PLAN_MISMATCH"


# ---------------------------------------------------------------------------
# 5. 已确认行程复核：按目标修订误差带复核原占用分钟
# ---------------------------------------------------------------------------

def test_review_depth_violation_uses_error_band(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    # 原占用分钟 0,1 在 rev1 安全（4+3=7>=6.5）

    # 第 0 分钟误差 0.6：低潮位 2.4，4+2.4=6.4 < 6.5 -> 水深违规
    _add_error_revision(client, {0: 0.6})
    data = _review(client, trip["trip_id"]).json()
    assert data["safe"] is False
    v = data["first_violation"]
    assert v["minute"] == 0
    assert v["reasons"] == ["depth"]
    assert v["tide_level"] == 3.0
    assert v["tide_error"] == 0.6
    assert v["available_depth"] == 6.4                      # 静水深度 + 名义 − 误差
    assert v["required_depth"] == 6.5


def test_review_clearance_violation_uses_error_band(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0, clearance=5.0)
    rev1 = seed_initial_revision(_level(0, 19, 0.0))
    trip = _plan_confirm(client, rev1, height=3.0, margin=1.0)

    # 第 1 分钟误差 1.5：高潮位 1.5，净空 5-1.5=3.5 < 4 -> 净空违规
    _add_error_revision(client, {1: 1.5})
    data = _review(client, trip["trip_id"]).json()
    v = data["first_violation"]
    assert v["minute"] == 1
    assert v["reasons"] == ["clearance"]
    assert v["available_clearance"] == 3.5                # 净空 − 名义 − 误差
    assert v["required_clearance"] == 4.0


def test_review_reschedule_uses_error_band(client):
    # 单段航时 2、深 4m；占用 0,1 的快照。误差使第 0 分钟不可行、2 分钟起可行，
    # 改期须推迟到第 2 分钟出发（占用 2,3）。
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    assert [l["start_minute"] for l in trip["itinerary"]] == [0]

    _add_error_revision(client, {0: 0.6, 1: 0.6})
    data = _review(client, trip["trip_id"]).json()
    assert data["safe"] is False
    assert data["no_solution"] is False
    rs = data["reschedule"]
    assert rs["revision"] == data["target_revision"]
    assert [l["start_minute"] for l in rs["itinerary"]] == [2]
    assert rs["arrival_minute"] == 4
    assert rs["changed_leg_count"] == 1
    # 快照不被改写
    g = client.get(f"/api/trips/{trip['trip_id']}").json()
    assert g["revision"] == rev1
    assert g["itinerary"] == trip["itinerary"]


def test_review_safe_when_error_still_within_band(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)

    # 误差 0.4：4+3-0.4=6.6>=6.5，原占用分钟仍安全
    _add_error_revision(client, {0: 0.4, 1: 0.4})
    data = _review(client, trip["trip_id"]).json()
    assert data["safe"] is True
    assert data["reschedule"] is None


# ---------------------------------------------------------------------------
# 6. 报错：未知基础修订 / 负误差 / 目标窗口（基础修订）缺名义潮位
# ---------------------------------------------------------------------------

def test_error_revision_unknown_base(client):
    seed_initial_revision(_level(0, 19, 3.0))
    r = _add_error_revision(client, {0: 0.1}, base_revision=999)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TIDE_REVISION_NOT_FOUND"
    assert r.json()["error"]["details"]["revision"] == 999


def test_error_revision_negative_error_rejected(client):
    seed_initial_revision(_level(0, 19, 3.0))
    r = _add_error_revision(client, {0: -0.01, 5: -2.0})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    negs = {(d["minute"], d["error"]) for d in err["details"]["negative_errors"]}
    assert negs == {(0, -0.01), (5, -2.0)}


def test_error_revision_negative_minute_rejected(client):
    seed_initial_revision(_level(0, 19, 3.0))
    r = _add_error_revision(client, {-1: 0.1})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_error_revision_missing_nominal_tide_rejected(client):
    # 稀疏修订只有 0..9；对第 12 分钟提交误差修订 -> MISSING_TIDE_DATA
    rev = seed_initial_revision(_level(0, 9, 3.0))
    r = _add_error_revision(client, {5: 0.2, 12: 0.3}, base_revision=rev)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "MISSING_TIDE_DATA"
    assert err["details"]["missing_minutes"] == [12]
    assert err["details"]["total_missing"] == 1
    assert err["details"]["base_revision"] == rev

    # 失败不产生新修订
    revs = client.get("/api/tide/revisions").json()
    assert [x["revision"] for x in revs] == [rev]


def test_error_revision_empty_rejected(client):
    seed_initial_revision(_level(0, 19, 3.0))
    r = _add_error_revision(client, {})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
