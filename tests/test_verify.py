"""参考仪温度核对自测：上传替换、唯一配对、超限、冲突、缺失、不复用、版本一致性。"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

REC_HEADER = "segment,local_seconds,temperature\n"
REF_HEADER = "utc,temperature\n"


def make_client(tmp_path, monkeypatch, worker="0", delay="0", max_ref=None) -> TestClient:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("WORKER_ENABLED", worker)
    monkeypatch.setenv("RECALC_DELAY", delay)
    if max_ref is not None:
        monkeypatch.setenv("MAX_REF_POINTS", max_ref)
    from app.main import create_app
    return TestClient(create_app())


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch) as c:
        yield c


def upload_recorder(client, rows, header=REC_HEADER):
    content = header + "".join(f"{s},{sec},{t}\n" for s, sec, t in rows)
    return client.post("/api/uploads", files={"file": ("rec.csv", content, "text/csv")})


def set_anchors(client, seg, l1, u1, l2, u2, version=None):
    payload = {"segment": seg, "local1": l1, "utc1": u1, "local2": l2, "utc2": u2}
    if version is not None:
        payload["version"] = version
    return client.post("/api/anchors", json=payload)


def upload_ref(client, rows, dev="30", lim="0.5", header=REF_HEADER):
    content = (header or "") + "".join(f"{u},{t}\n" for u, t in rows)
    return client.post("/api/reference",
                       files={"file": ("ref.csv", content, "text/csv")},
                       data={"deviation_s": dev, "temp_limit": lim})


def wait_idle(client, timeout=30.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        calc = client.get("/api/status").json()["calc"]
        if calc["status"] != "running" and not calc["dirty"]:
            return calc
        time.sleep(0.05)
    raise AssertionError("重算未在超时内收敛")


def setup_basic(client):
    """3 枚记录仪读数（整 2/5/8 分钟）+ 3 枚对齐参考点，全部应唯一配对。"""
    upload_recorder(client, [(1, 120, 4.0), (1, 150, 4.1), (1, 180, 4.2)])
    set_anchors(client, 1, 100, "2026-01-01T00:00:00Z", 200, "2026-01-01T00:10:00Z")
    upload_ref(client, [("2026-01-01T00:02:00Z", 4.0),
                        ("2026-01-01T00:05:00Z", 4.15),
                        ("2026-01-01T00:08:00Z", 4.2)])


def make_unit_db(tmp_path, monkeypatch):
    """不经 HTTP 直接建库：3 枚已校读数 + 3 枚参考点，返回 (cfg, db)。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "unit.db"))
    monkeypatch.setenv("WORKER_ENABLED", "0")
    from app import services
    from app.calibration import RecalcWorker
    from app.config import Settings
    from app.db import Database
    from app.timeutil import parse_utc

    cfg = Settings.from_env()
    db = Database(cfg.db_path)
    services.add_batch(db, cfg, "rec.csv", [
        {"row_no": 1, "segment": 1, "local_seconds": 120, "temperature": 4.0},
        {"row_no": 2, "segment": 1, "local_seconds": 150, "temperature": 4.1},
        {"row_no": 3, "segment": 1, "local_seconds": 180, "temperature": 4.2},
    ])
    services.set_anchors(db, 1, 100, parse_utc("2026-01-01T00:00:00Z"),
                         200, parse_utc("2026-01-01T00:10:00Z"), None)
    RecalcWorker(db).run_until_quiet()
    services.replace_reference(db, cfg, "ref.csv", [
        {"row_no": 1, "utc": parse_utc("2026-01-01T00:02:00Z"), "temperature": 4.0},
        {"row_no": 2, "utc": parse_utc("2026-01-01T00:05:00Z"), "temperature": 4.15},
        {"row_no": 3, "utc": parse_utc("2026-01-01T00:08:00Z"), "temperature": 4.2},
    ], 30.0, 0.5)
    return cfg, db


# ---------------------------------------------------------------- 参考点上传

def test_ref_upload_happy_and_replace(client):
    r = upload_ref(client, [("2026-01-01T00:02:00Z", 4.0),
                            ("2026-01-01T00:05:00Z", 4.1)], dev="45", lim="0.8")
    assert r.status_code == 200, r.text
    assert r.json() == {"ref_batch_id": 1, "accepted": 2}
    info = client.get("/api/reference").json()
    assert info["count"] == 2
    assert info["window"] == {"start": "2026-01-01T00:02:00.000Z",
                              "end": "2026-01-01T00:05:00.000Z"}
    assert info["deviation_s"] == 45.0 and info["temp_limit"] == 0.8

    # 再次上传：整体替换而非累加
    r = upload_ref(client, [("2026-01-02T08:00:00Z", -1.5)])
    assert r.status_code == 200 and r.json()["accepted"] == 1
    info = client.get("/api/reference").json()
    assert info["count"] == 1
    assert info["window"]["start"] == "2026-01-02T08:00:00.000Z"
    assert info["deviation_s"] == 30.0 and info["temp_limit"] == 0.5  # 参数随新上传更新


def test_ref_upload_headers_and_no_header(client):
    content = "﻿时间,温度\n2026-01-01 00:02:00,4.0\n"
    r = client.post("/api/reference",
                    files={"file": ("r.csv", content.encode("utf-8"), "text/csv")},
                    data={"deviation_s": "30", "temp_limit": "0.5"})
    assert r.status_code == 200, r.text
    # 无表头（固定顺序 utc,temperature）
    r = upload_ref(client, [("2026-01-01T00:02:00Z", 4.0),
                            ("2026-01-01T00:05:00.500Z", 4.1)], header="")
    assert r.status_code == 200 and r.json()["accepted"] == 2


def test_ref_upload_validation(client):
    # 坏行带行号，整批拒绝
    content = "utc,temperature\nnot-a-time,4.0\n2026-01-01T00:02:00Z,abc\n"
    r = client.post("/api/reference",
                    files={"file": ("r.csv", content, "text/csv")},
                    data={"deviation_s": "30", "temp_limit": "0.5"})
    assert r.status_code == 400 and r.json()["code"] == "CSV_INVALID"
    assert {e["row"] for e in r.json()["errors"]} == {1, 2}
    assert client.get("/api/reference").json()["count"] == 0
    # 无法识别表头
    r = upload_ref(client, [("hello", "world")], header="")
    assert r.status_code == 400
    # 非法核对参数
    for dev, lim in [("0", "0.5"), ("-1", "0.5"), ("abc", "0.5"), ("30", "0"), ("30", "x")]:
        r = upload_ref(client, [("2026-01-01T00:02:00Z", 4.0)], dev=dev, lim=lim)
        assert r.status_code == 400 and r.json()["code"] == "BAD_VERIFY_PARAM", (dev, lim)


def test_ref_upload_cap(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch, max_ref="3") as c:
        r = upload_ref(c, [("2026-01-01T00:02:00Z", 1.0),
                           ("2026-01-01T00:03:00Z", 1.0),
                           ("2026-01-01T00:04:00Z", 1.0),
                           ("2026-01-01T00:05:00Z", 1.0)])
        assert r.status_code == 400
        assert c.get("/api/reference").json()["count"] == 0


# ---------------------------------------------------------------- 配对与判定

def test_unique_pairs_consistent(client):
    setup_basic(client)
    r = client.get("/api/verify").json()
    assert r["currency"] == "current"
    assert r["verdict"] == "consistent" and r["qualified"] is True
    assert r["counts"]["pairs"] == 3
    assert r["counts"]["over_limit"] == 0
    # 两侧原行号、批次号、锚点版本齐全；温差 = 记录仪 − 参考仪
    by_ref_row = {p["ref"]["row_no"]: p for p in r["pairs"]}
    assert by_ref_row[1]["rec"]["row_no"] == 1
    assert by_ref_row[2]["rec"]["row_no"] == 2
    assert by_ref_row[3]["rec"]["row_no"] == 3
    for p in r["pairs"]:
        assert p["rec"]["batch_id"] == 1
        assert p["rec"]["anchor_version"] == 1
        assert p["rec"]["calc_version"] == r["calc_version"]
        assert p["ref"]["ref_batch_id"] == 1
    assert by_ref_row[2]["temp_diff"] == -0.05   # 4.1 − 4.15
    assert by_ref_row[1]["time_delta_s"] == 0


def test_over_limit_blocks_consistent(client):
    upload_recorder(client, [(1, 120, 4.0), (1, 150, 4.1), (1, 180, 4.2)])
    set_anchors(client, 1, 100, "2026-01-01T00:00:00Z", 200, "2026-01-01T00:10:00Z")
    upload_ref(client, [("2026-01-01T00:02:00Z", 4.0),
                        ("2026-01-01T00:05:00Z", 4.1),
                        ("2026-01-01T00:08:00Z", 5.0)])   # 温差 4.2−5.0 = −0.8 超限
    r = client.get("/api/verify").json()
    assert r["verdict"] == "not_consistent" and r["qualified"] is False
    assert r["counts"]["over_limit"] == 1
    item = r["over_limit"][0]
    assert item["ref"]["row_no"] == 3 and item["rec"]["row_no"] == 3
    assert item["temp_diff"] == -0.8
    # 边界：|温差| 恰等于限值不算超限
    upload_ref(client, [("2026-01-01T00:02:00Z", 4.0),
                        ("2026-01-01T00:05:00Z", 4.1),
                        ("2026-01-01T00:08:00Z", 4.7)])   # |−0.5| = 限值 0.5 → 合格
    r = client.get("/api/verify").json()
    assert r["verdict"] == "consistent" and r["qualified"] is True


def test_conflict_multiple_candidates(client):
    # 1 秒/本地秒：读数 00:00:00..00:00:10；一个参考点落在多个读数偏差内
    upload_recorder(client, [(1, 100 + i, 4.0) for i in range(11)])
    set_anchors(client, 1, 100, "2026-01-01T00:00:00Z", 200, "2026-01-01T00:01:40Z")
    upload_ref(client, [("2026-01-01T00:00:05Z", 4.0)], dev="3")
    r = client.get("/api/verify").json()
    assert r["verdict"] == "not_consistent" and r["qualified"] is False
    assert r["counts"]["pairs"] == 0
    assert r["counts"]["conflict_clusters"] == 1
    assert r["counts"]["conflict_refs"] == 1
    assert r["counts"]["conflict_recs"] == 7   # 00:00:02..00:00:08
    cluster = r["conflicts"][0]
    assert [x["row_no"] for x in cluster["refs"]] == [1]
    assert len(cluster["recs"]) == 7
    # 簇内记录仪侧保留批次号与锚点版本
    assert cluster["recs"][0]["batch_id"] == 1
    assert cluster["recs"][0]["anchor_version"] == 1


def test_conflict_two_refs_one_rec_no_reuse(client):
    # 两个参考点争同一枚读数：整簇冲突，读数不得复用为两对
    upload_recorder(client, [(1, 105, 4.0)])     # 唯一读数 00:00:05
    set_anchors(client, 1, 100, "2026-01-01T00:00:00Z", 200, "2026-01-01T00:01:40Z")
    upload_ref(client, [("2026-01-01T00:00:05Z", 4.0),
                        ("2026-01-01T00:00:05.500Z", 4.0)], dev="1")
    r = client.get("/api/verify").json()
    assert r["counts"]["pairs"] == 0
    assert r["counts"]["conflict_clusters"] == 1
    assert r["counts"]["conflict_refs"] == 2
    assert r["counts"]["conflict_recs"] == 1
    assert r["qualified"] is False


def test_conflict_chain_not_greedy(client):
    # 链式候选 ref0-rec1-ref2-rec3：任一侧都有多候选，整簇冲突而非贪心配对
    upload_recorder(client, [(1, 101, 4.0), (1, 103, 4.0)])
    set_anchors(client, 1, 100, "2026-01-01T00:00:00Z", 200, "2026-01-01T00:01:40Z")
    upload_ref(client, [("2026-01-01T00:00:00Z", 4.0),
                        ("2026-01-01T00:00:02Z", 4.0)], dev="1.5")
    r = client.get("/api/verify").json()
    assert r["counts"]["pairs"] == 0
    assert r["counts"]["conflict_clusters"] == 1
    assert r["counts"]["conflict_refs"] == 2 and r["counts"]["conflict_recs"] == 2


def test_missing_both_sides(client):
    # 读数在整 0..10 分钟；参考点只覆盖 00:00 与 00:05，另有一枚落在空档 00:02:30
    upload_recorder(client, [(1, 100 + 10 * i, 4.0) for i in range(11)])
    set_anchors(client, 1, 100, "2026-01-01T00:00:00Z", 200, "2026-01-01T00:10:00Z")
    upload_ref(client, [("2026-01-01T00:00:00Z", 4.0),
                        ("2026-01-01T00:02:30Z", 4.0),
                        ("2026-01-01T00:05:00Z", 4.0)], dev="10")
    r = client.get("/api/verify").json()
    assert r["verdict"] == "not_consistent" and r["qualified"] is False
    assert r["counts"]["pairs"] == 2                       # 00:00 与 00:05
    # 参考点 00:02:30 无记录仪候选 → 记录仪侧缺失
    assert r["counts"]["missing_refs"] == 1
    assert r["missing"]["recorder_side"][0]["row_no"] == 2
    # 窗口 [00:00, 00:05] 内 00:01..00:04 的读数无参考候选 → 参考侧缺失
    assert r["counts"]["missing_recs"] == 4
    assert {m["row_no"] for m in r["missing"]["reference_side"]} == {2, 3, 4, 5}
    # 窗口外读数（00:06..00:10）不计缺失
    assert r["counts"]["recorders_out_of_window"] == 5


def test_out_of_window_recorders_ignored(client):
    upload_recorder(client, [(1, 100 + 10 * i, 4.0) for i in range(11)])
    set_anchors(client, 1, 100, "2026-01-01T00:00:00Z", 200, "2026-01-01T00:10:00Z")
    upload_ref(client, [("2026-01-01T00:04:00Z", 4.0),
                        ("2026-01-01T00:05:00Z", 4.0),
                        ("2026-01-01T00:06:00Z", 4.0)])
    r = client.get("/api/verify").json()
    assert r["verdict"] == "consistent" and r["qualified"] is True
    assert r["counts"]["pairs"] == 3
    assert r["counts"]["recorders_in_window"] == 3
    assert r["counts"]["recorders_out_of_window"] == 8
    assert r["window"] == {"start": "2026-01-01T00:04:00.000Z",
                           "end": "2026-01-01T00:06:00.000Z"}


def test_deviation_boundary_inclusive(client):
    upload_recorder(client, [(1, 120, 4.0)])     # 唯一读数 00:02:00
    set_anchors(client, 1, 100, "2026-01-01T00:00:00Z", 200, "2026-01-01T00:10:00Z")
    upload_ref(client, [("2026-01-01T00:02:30Z", 4.0)], dev="30")  # 恰在边界 30s
    r = client.get("/api/verify").json()
    assert r["counts"]["pairs"] == 1
    assert r["pairs"][0]["time_delta_s"] == -30
    # 偏差收紧到 29s：参考点无候选 → 记录仪侧缺失；
    # 窗口 [00:01:30, 00:02:30] 内的读数无候选 → 参考侧缺失
    upload_ref(client, [("2026-01-01T00:01:30Z", 4.0),
                        ("2026-01-01T00:02:30Z", 4.0)], dev="29")
    r = client.get("/api/verify").json()
    assert r["counts"]["pairs"] == 0
    assert r["counts"]["missing_refs"] == 2 and r["counts"]["missing_recs"] == 1


def test_verify_param_override_not_persisted(client):
    setup_basic(client)
    r = client.get("/api/verify", params={"deviation_s": 0.001}).json()
    assert r["counts"]["pairs"] == 3   # 对时差为 0，仍配对
    info = client.get("/api/reference").json()
    assert info["deviation_s"] == 30.0  # 查询参数不落库


def test_no_reference_data(client):
    r = client.get("/api/verify").json()
    assert r["verdict"] == "no_reference" and r["qualified"] is False
    assert r["counts"]["refs_total"] == 0


def test_no_calibrated_readings(client):
    upload_ref(client, [("2026-01-01T00:02:00Z", 4.0)])
    r = client.get("/api/verify").json()
    assert r["verdict"] == "not_consistent" and r["qualified"] is False
    assert r["counts"]["missing_refs"] == 1


# ---------------------------------------------------------------- 版本一致性

def test_stale_while_recalc_pending(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch, worker="1", delay="5") as c:
        setup_basic(c)
        wait_idle(c)
        r = c.get("/api/verify").json()
        assert r["currency"] == "current" and r["qualified"] is True

        # 上传窗外新点（不影响配对结论）→ 重算未追平期间只能旧版回看
        upload_recorder(c, [(1, 199, 9.9)])
        r = c.get("/api/verify").json()
        assert r["currency"] == "stale"
        assert r["verdict"] == "consistent"      # 配对本身仍一致
        assert r["qualified"] is False           # 但绝不能显示当前合格
        assert any("未追平" in s for s in r["stale_reasons"])

        wait_idle(c)
        r = c.get("/api/verify").json()
        assert r["currency"] == "current" and r["qualified"] is True


def test_stale_after_anchor_correction(tmp_path, monkeypatch):
    cfg, db = make_unit_db(tmp_path, monkeypatch)
    from app import services, verify
    from app.timeutil import parse_utc
    res = verify.run_verification(db, 30.0, 0.5)
    assert res["currency"] == "current" and res["qualified"] is True
    # 更正锚点（直接落库、尚未重算）→ 只能旧版回看
    services.set_anchors(db, 1, 100, parse_utc("2026-01-01T01:00:00Z"),
                         200, parse_utc("2026-01-01T01:10:00Z"), version=1)
    res = verify.run_verification(db, 30.0, 0.5)
    assert res["currency"] == "stale" and res["qualified"] is False
    assert any("未追平" in s for s in res["stale_reasons"])
    db.close()


def test_mid_verify_version_change_retried(tmp_path, monkeypatch):
    cfg, db = make_unit_db(tmp_path, monkeypatch)
    from app import verify
    state = {"n": 0}

    def flaky_stamp():
        state["n"] += 1
        v, g, c = verify._version_stamp(db)
        if state["n"] == 2:      # 第一次核验的“后读”：模拟期间发生换版
            return v + 1, g, c
        return v, g, c

    res = verify.run_verification(db, 30.0, 0.5, stamp_fn=flaky_stamp)
    assert res["attempts"] == 2                    # 换版触发重试
    assert res["currency"] == "current"
    assert res["qualified"] is True
    db.close()


def test_persistent_version_change_marks_stale(tmp_path, monkeypatch):
    cfg, db = make_unit_db(tmp_path, monkeypatch)
    from app import verify
    state = {"n": 0}

    def always_changing():
        state["n"] += 1
        v, g, c = verify._version_stamp(db)
        return v + state["n"], g, c                # 每次读都不同：版本持续变化

    res = verify.run_verification(db, 30.0, 0.5, stamp_fn=always_changing)
    assert res["attempts"] == 3                    # 重试 2 次后共 3 次，仍不稳定
    assert res["currency"] == "stale"
    assert res["qualified"] is False
    assert any("变化" in s for s in res["stale_reasons"])
    db.close()


# ---------------------------------------------------------------- 页面

def test_verify_pages_render(client):
    r = client.get("/verify")
    assert r.status_code == 200 and "尚无参考点" in r.text
    setup_basic(client)
    r = client.get("/verify")
    assert r.status_code == 200
    assert "一致（温度核对合格）" in r.text
    assert "配对温差" in r.text and "批次号" in r.text and "锚点版本" in r.text
    r = client.get("/reference")
    assert r.status_code == 200 and "当前参考点集" in r.text
    r = client.get("/")
    assert r.status_code == 200 and "参考仪数据" in r.text


def test_verify_page_shows_not_consistent(client):
    upload_recorder(client, [(1, 120, 4.0)])
    set_anchors(client, 1, 100, "2026-01-01T00:00:00Z", 200, "2026-01-01T00:10:00Z")
    upload_ref(client, [("2026-01-01T00:02:00Z", 9.9)])   # 温差 5.9 超限
    r = client.get("/verify")
    assert r.status_code == 200
    assert "不一致" in r.text and "超限项" in r.text


def test_verify_page_renders_conflicts_and_missing(client):
    # 两个冲突簇（00:00:02、00:00:08 各 1 对多）+ 窗口内无候选读数 + 无候选参考点
    upload_recorder(client, [(1, 100 + i, 4.0) for i in range(11)])
    set_anchors(client, 1, 100, "2026-01-01T00:00:00Z", 200, "2026-01-01T00:01:40Z")
    upload_ref(client, [("2026-01-01T00:00:02Z", 4.0),
                        ("2026-01-01T00:00:08Z", 4.0),
                        ("2026-01-01T00:00:20Z", 4.0)],   # 无任何候选 → 记录仪侧缺失
               dev="1")
    r = client.get("/api/verify").json()
    assert r["counts"]["conflict_clusters"] == 2           # {ref02,rec01..03} {ref08,rec07..09}
    assert r["counts"]["missing_refs"] == 1                # 00:00:20 的参考点
    # 窗口 [00:00:02, 00:00:20] 内 00:00:04..06 与 00:00:10 的读数无候选
    assert r["counts"]["missing_recs"] == 4
    html = client.get("/verify").text
    assert "冲突簇" in html
    assert "缺失：参考点无对应记录仪读数" in html
    assert "缺失：记录仪读数无对应参考点" in html
    assert "不一致" in html


def test_verify_params_form_persists(client):
    setup_basic(client)
    r = client.post("/verify", data={"deviation_s": "12", "temp_limit": "0.3"},
                    follow_redirects=False)
    assert r.status_code == 303
    info = client.get("/api/reference").json()
    assert info["deviation_s"] == 12.0 and info["temp_limit"] == 0.3
    # 非法参数 → 页面报错且不落库
    r = client.post("/verify", data={"deviation_s": "abc", "temp_limit": "0.3"})
    assert r.status_code == 200 and "数值" in r.text
    info = client.get("/api/reference").json()
    assert info["deviation_s"] == 12.0
