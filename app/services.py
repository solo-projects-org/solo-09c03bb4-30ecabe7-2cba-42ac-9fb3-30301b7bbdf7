"""领域服务：上传入库、锚点维护、查询、状态汇总。

所有写操作在单事务内完成并递增 state_generation（触发后续重算）。
参考点集（ref_points）只服务于温度核对、不影响校时结果，
因此替换参考集与保存核对参数不递增 state_generation。
"""
from __future__ import annotations

import math
from datetime import datetime

from .config import Settings
from .db import Database
from .timeutil import fmt_utc, utcnow_iso


class ServiceError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int = 400,
        code: str = "BAD_REQUEST",
        errors: list | None = None,
        current_version: int | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.errors = errors or []
        self.current_version = current_version


def _bump_generation(cur) -> None:
    cur.execute(
        "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
        " WHERE key = 'state_generation'"
    )


# ---------------------------------------------------------------- 上传

def add_batch(db: Database, cfg: Settings, filename: str, rows: list[dict]) -> int:
    """整批入库（原子）：任一点冲突或超上限则整批拒绝。返回批次号。"""
    if not rows:
        raise ServiceError("没有可入库的数据行", code="EMPTY_BATCH")
    with db.tx() as cur:
        total = cur.execute("SELECT COUNT(*) AS c FROM readings").fetchone()["c"]
        if total + len(rows) > cfg.max_points:
            raise ServiceError(
                f"累计点数将超过上限 {cfg.max_points}（现有 {total} 点，本批 {len(rows)} 行），整批已拒绝",
                code="POINT_LIMIT_EXCEEDED",
            )
        conflicts = []
        for r in rows:
            hit = cur.execute(
                "SELECT 1 FROM readings WHERE segment = ? AND local_seconds = ?",
                (r["segment"], r["local_seconds"]),
            ).fetchone()
            if hit:
                conflicts.append({
                    "row": r["row_no"],
                    "message": f"与已有数据重复：段 {r['segment']} 秒数 {r['local_seconds']}",
                })
        if conflicts:
            raise ServiceError(
                f"发现 {len(conflicts)} 条同段秒数重复，整批已拒绝",
                code="DUPLICATE_SECONDS",
                errors=conflicts,
            )
        now = utcnow_iso()
        batch_id = cur.execute(
            "INSERT INTO batches(filename, created_at, rows_accepted) VALUES (?, ?, ?)",
            (filename[:200], now, len(rows)),
        ).lastrowid
        cur.executemany(
            "INSERT INTO readings(batch_id, row_no, segment, local_seconds, temperature, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (batch_id, r["row_no"], r["segment"], r["local_seconds"], r["temperature"], now)
                for r in rows
            ],
        )
        _bump_generation(cur)
    return batch_id


# ---------------------------------------------------------------- 锚点

def set_anchors(
    db: Database,
    segment: int,
    local1: int,
    utc1,                      # datetime
    local2: int,
    utc2,                      # datetime
    version: int | None,       # 所见版本；新建时须为 None
) -> int:
    """录入或更正某段的两枚锚点，返回新版本号。

    约束：0 ≤ local1 < local2 且 utc1 < utc2（UTC 须递增）。
    更正时必须提交所见版本，否则 409。
    """
    if segment < 0:
        raise ServiceError("段号须为非负整数", code="BAD_SEGMENT")
    if not (0 <= local1 < local2):
        raise ServiceError("本地秒数须满足 0 ≤ 秒数1 < 秒数2", code="BAD_LOCAL_RANGE")
    if not (utc1 < utc2):
        raise ServiceError("UTC 必须递增：UTC2 须晚于 UTC1", code="UTC_NOT_INCREASING")

    with db.tx() as cur:
        row = cur.execute(
            "SELECT version FROM anchors WHERE segment = ?", (segment,)
        ).fetchone()
        now = utcnow_iso()
        if row is None:
            if version is not None:
                raise ServiceError(
                    "该段尚无锚点，无法按版本更正（请清空版本字段按新建提交）",
                    status=409, code="VERSION_CONFLICT", current_version=None,
                )
            new_version = 1
            cur.execute(
                "INSERT INTO anchors(segment, local1, utc1, local2, utc2, version, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (segment, local1, fmt_utc(utc1), local2, fmt_utc(utc2), new_version, now),
            )
        else:
            current = row["version"]
            if version is None or version != current:
                raise ServiceError(
                    f"锚点版本不一致：提交版本 {version}，当前版本 {current}，请刷新后重试",
                    status=409, code="VERSION_CONFLICT", current_version=current,
                )
            new_version = current + 1
            cur.execute(
                "UPDATE anchors SET local1 = ?, utc1 = ?, local2 = ?, utc2 = ?,"
                " version = ?, updated_at = ? WHERE segment = ?",
                (local1, fmt_utc(utc1), local2, fmt_utc(utc2), new_version, now, segment),
            )
        _bump_generation(cur)
    return new_version


def list_anchors(db: Database) -> list[dict]:
    anchors = [dict(a) for a in db.query("SELECT * FROM anchors ORDER BY segment")]
    secs_by_seg: dict[int, list[int]] = {}
    for r in db.query("SELECT segment, local_seconds FROM readings"):
        secs_by_seg.setdefault(r["segment"], []).append(r["local_seconds"])
    for a in anchors:
        secs = secs_by_seg.get(a["segment"], [])
        a["point_count"] = len(secs)
        a["in_range_count"] = sum(1 for s in secs if a["local1"] <= s <= a["local2"])
    return anchors


# ---------------------------------------------------------------- 查询

def calc_info(db: Database) -> dict:
    row = db.query_one("SELECT * FROM calc_state WHERE id = 1")
    meta = {r["key"]: r["value"] for r in db.query("SELECT key, value FROM meta")}
    state_gen = int(meta.get("state_generation", "0"))
    calibrated_gen = int(meta.get("calibrated_generation", "-1"))
    return {
        "version": row["version"],
        "status": row["status"],
        "last_error": row["last_error"],
        "updated_at": row["updated_at"],
        "state_generation": state_gen,
        "calibrated_generation": calibrated_gen,
        "dirty": state_gen != calibrated_gen,
    }


def query_by_date(db: Database, date_str: str) -> dict:
    try:
        day = datetime.strptime((date_str or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ServiceError("日期格式须为 YYYY-MM-DD", code="BAD_DATE")
    info = calc_info(db)
    rows = db.query(
        """
        SELECT c.reading_id, c.utc_time, c.anchor_version, c.calc_version,
               r.batch_id, r.row_no, r.segment, r.local_seconds, r.temperature
        FROM calibrated c JOIN readings r ON r.id = c.reading_id
        WHERE c.utc_date = ?
        ORDER BY c.utc_time, c.reading_id
        """,
        (day.isoformat(),),
    )
    return {
        "date": day.isoformat(),
        "calc_version": info["version"],
        "dirty": info["dirty"],
        "count": len(rows),
        "readings": [dict(r) for r in rows],
    }


def pending_readings(db: Database) -> list[dict]:
    """尚未进入当前校时版本的读数（无锚点或超出锚点范围），即“留存待校时”。"""
    anchors = {a["segment"]: a for a in db.query("SELECT * FROM anchors")}
    out = []
    for r in db.query(
        "SELECT id, batch_id, row_no, segment, local_seconds, temperature"
        " FROM readings ORDER BY id"
    ):
        a = anchors.get(r["segment"])
        if a is None:
            reason = "该段未设置校时锚点"
        elif not (a["local1"] <= r["local_seconds"] <= a["local2"]):
            reason = f"超出锚点范围 [{a['local1']}, {a['local2']}]"
        else:
            continue
        out.append({**dict(r), "reason": reason})
    return out


def get_status(db: Database, cfg: Settings) -> dict:
    accepted = db.query_one("SELECT COUNT(*) AS c FROM readings")["c"]
    batches = [
        dict(b) for b in db.query("SELECT * FROM batches ORDER BY id DESC LIMIT 100")
    ]
    return {
        "points": {"accepted": accepted, "max": cfg.max_points},
        "calc": calc_info(db),
        "anchors": list_anchors(db),
        "batches": batches,
        "pending_count": len(pending_readings(db)),
        "reference": reference_info(db, cfg),
    }


# ---------------------------------------------------------------- 参考仪核对

def validate_verify_params(deviation_s, temp_limit) -> tuple[float, float]:
    """校验允许对时偏差（秒）与温差限值（℃），返回规范化后的浮点值。"""
    try:
        dev = float(str(deviation_s).strip())
        lim = float(str(temp_limit).strip())
    except (TypeError, ValueError):
        raise ServiceError("允许对时偏差与温差限值须为数值", code="BAD_VERIFY_PARAM")
    if not math.isfinite(dev) or not (0 < dev <= 86400):
        raise ServiceError("允许对时偏差须为 (0, 86400] 秒", code="BAD_VERIFY_PARAM")
    if not math.isfinite(lim) or not (0 < lim <= 100):
        raise ServiceError("温差限值须为 (0, 100] ℃", code="BAD_VERIFY_PARAM")
    return dev, lim


def replace_reference(
    db: Database,
    cfg: Settings,
    filename: str,
    rows: list[dict],          # [{"row_no", "utc"(datetime), "temperature"}]
    deviation_s: float,
    temp_limit: float,
) -> tuple[int, int]:
    """整体替换参考点集（单事务），并保存核对参数。返回 (参考批次号, 接纳行数)。

    参考集只影响温度核对、不影响校时结果，故不递增 state_generation。
    """
    if not rows:
        raise ServiceError("没有可入库的数据行", code="EMPTY_BATCH")
    if len(rows) > cfg.max_ref_points:
        raise ServiceError(
            f"参考点超过上限 {cfg.max_ref_points}（本批 {len(rows)} 行），整批已拒绝",
            code="POINT_LIMIT_EXCEEDED",
        )
    with db.tx() as cur:
        now = utcnow_iso()
        batch_id = cur.execute(
            "INSERT INTO ref_batches(filename, created_at, rows_accepted) VALUES (?, ?, ?)",
            (filename[:200], now, len(rows)),
        ).lastrowid
        cur.execute("DELETE FROM ref_points")  # 整体替换：旧参考集不再参与核对
        cur.executemany(
            "INSERT INTO ref_points(batch_id, row_no, utc_time, utc_ts, temperature, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    batch_id,
                    r["row_no"],
                    fmt_utc(r["utc"]),
                    round(r["utc"].timestamp(), 3),   # 毫秒精度，与校时结果一致
                    r["temperature"],
                    now,
                )
                for r in rows
            ],
        )
        cur.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('ref_deviation_s', ?)",
            (repr(deviation_s),),
        )
        cur.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('ref_temp_limit', ?)",
            (repr(temp_limit),),
        )
    return batch_id, len(rows)


def get_verify_params(db: Database, cfg: Settings) -> tuple[float, float]:
    """当前核对参数（允许对时偏差秒数、温差限值℃）；未设置时用配置默认值。"""
    meta = {r["key"]: r["value"] for r in db.query("SELECT key, value FROM meta")}
    dev = float(meta.get("ref_deviation_s", repr(cfg.ref_deviation_s)))
    lim = float(meta.get("ref_temp_limit", repr(cfg.ref_temp_limit)))
    return dev, lim


def set_verify_params(db: Database, deviation_s: float, temp_limit: float) -> None:
    """保存核对参数（不影响校时，不递增 state_generation）。"""
    with db.tx() as cur:
        cur.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('ref_deviation_s', ?)",
            (repr(deviation_s),),
        )
        cur.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('ref_temp_limit', ?)",
            (repr(temp_limit),),
        )


def reference_info(db: Database, cfg: Settings) -> dict:
    """参考点集概况：点数、覆盖时段、核对参数、最近上传批次。"""
    count = db.query_one("SELECT COUNT(*) AS c FROM ref_points")["c"]
    window = None
    if count:
        row = db.query_one(
            "SELECT MIN(utc_time) AS lo, MAX(utc_time) AS hi FROM ref_points"
        )
        window = {"start": row["lo"], "end": row["hi"]}
    batch = db.query_one("SELECT * FROM ref_batches ORDER BY id DESC LIMIT 1")
    dev, lim = get_verify_params(db, cfg)
    return {
        "count": count,
        "max": cfg.max_ref_points,
        "window": window,
        "deviation_s": dev,
        "temp_limit": lim,
        "last_batch": dict(batch) if batch else None,
    }
