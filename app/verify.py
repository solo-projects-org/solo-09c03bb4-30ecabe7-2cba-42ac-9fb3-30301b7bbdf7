"""参考仪温度核对：唯一配对、冲突/缺失判定与发布版一致性。

配对规则（连通分量法）：
- 候选边：|参考点 UTC − 校时读数 UTC| ≤ 允许对时偏差（闭区间）；
- 只有「双方各自只有一个候选」的 1-1 连通分量才配对，读数不会复用；
- 任一侧候选数 ≥ 2 的连通分量整体列为冲突；
- 没有任何候选的点列为缺失（区分参考侧缺、记录仪侧缺）。

覆盖时段（[min(参考点), max(参考点)]）的作用：
- 候选配对面向全体已发布校时读数，不被时段截断——否则单参考点或时段
  边缘的参考点永远无法配对；
- 缺失判定中，只有落在覆盖时段内的记录仪读数才计「参考侧缺失」；
  时段外的读数不属于本次核对范围，不计缺失、不影响合格判定。

发布版一致性：
- 核验前后各读一次 (校时版本, 数据代次, 已校代次)，不一致则重试，
  重试仍不稳定则标为「旧版回看」；
- 数据代次 ≠ 已校代次（有上传/锚点更正未追平）时同样只能标为「旧版回看」；
- 旧版回看下 qualified 恒为 False，绝不显示当前合格。
"""
from __future__ import annotations

import bisect

from .db import Database
from .timeutil import parse_utc

MAX_VERIFY_RETRIES = 2  # 核验期间发生换版时的额外重试次数
_EPS = 1e-6             # 时间比较的浮点容差（远小于毫秒精度）


# ---------------------------------------------------------------- 版本一致性

def _version_stamp(db: Database) -> tuple[int, int, int]:
    """一致读取 (校时版本, 数据代次, 已校代次)。"""
    def fn(conn):
        version = conn.execute(
            "SELECT version FROM calc_state WHERE id = 1"
        ).fetchone()["version"]
        meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
        return (
            version,
            int(meta.get("state_generation", "0")),
            int(meta.get("calibrated_generation", "-1")),
        )
    return db.snapshot(fn)


def _load_inputs(db: Database):
    """一致快照：当前发布版校时读数 + 全部参考点（同事务视图，必为同一完整版）。"""
    def fn(conn):
        recs = [dict(r) for r in conn.execute(
            """
            SELECT c.reading_id, c.utc_time, c.anchor_version, c.calc_version,
                   r.batch_id, r.row_no, r.segment, r.local_seconds, r.temperature
            FROM calibrated c JOIN readings r ON r.id = c.reading_id
            ORDER BY c.utc_time, c.reading_id
            """
        )]
        refs = [dict(r) for r in conn.execute(
            """
            SELECT p.id, p.batch_id, p.row_no, p.utc_time, p.utc_ts, p.temperature
            FROM ref_points p
            ORDER BY p.utc_ts, p.id
            """
        )]
        return refs, recs
    return db.snapshot(fn)


# ---------------------------------------------------------------- 配对（纯函数）

def match_points(refs: list[dict], recs: list[dict], deviation_s: float) -> dict:
    """对参考点与校时读数做唯一配对。

    refs/recs 元素须含 ``ts``（epoch 秒，float）。
    返回 {"pairs": [(ri, ci)], "conflicts": [{"refs": [...], "recs": [...]}],
          "missing_refs": [...], "missing_recs": [...]}（下标均指入参列表）。
    """
    order = sorted(range(len(recs)), key=lambda i: (recs[i]["ts"], i))
    sorted_ts = [recs[i]["ts"] for i in order]

    n, m = len(refs), len(recs)
    parent = list(range(n + m))  # 参考点 0..n-1，记录仪点 n..n+m-1

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for ri, ref in enumerate(refs):
        lo = bisect.bisect_left(sorted_ts, ref["ts"] - deviation_s - _EPS)
        hi = bisect.bisect_right(sorted_ts, ref["ts"] + deviation_s + _EPS)
        for k in range(lo, hi):
            union(ri, n + order[k])

    comps: dict[int, dict] = {}
    for ri in range(n):
        comps.setdefault(find(ri), {"refs": [], "recs": []})["refs"].append(ri)
    for ci in range(m):
        comps.setdefault(find(n + ci), {"refs": [], "recs": []})["recs"].append(ci)

    pairs, conflicts, missing_refs, missing_recs = [], [], [], []
    for comp in comps.values():
        rs, cs = comp["refs"], comp["recs"]
        if len(rs) == 1 and len(cs) == 1:
            pairs.append((rs[0], cs[0]))
        elif rs and cs:
            conflicts.append(comp)          # 任一侧多候选 → 整体冲突
        elif rs:
            missing_refs.extend(rs)         # 参考侧有点，记录仪侧缺失
        else:
            missing_recs.extend(cs)         # 记录仪侧有点，参考侧缺失
    return {
        "pairs": pairs,
        "conflicts": conflicts,
        "missing_refs": sorted(missing_refs, key=lambda i: (refs[i]["ts"], i)),
        "missing_recs": sorted(missing_recs, key=lambda i: (recs[i]["ts"], i)),
    }


# ---------------------------------------------------------------- 结果装配

def _pub_ref(r: dict) -> dict:
    return {
        "row_no": r["row_no"],
        "utc_time": r["utc_time"],
        "temperature": r["temperature"],
        "ref_batch_id": r["batch_id"],
    }


def _pub_rec(r: dict) -> dict:
    return {
        "batch_id": r["batch_id"],
        "row_no": r["row_no"],
        "segment": r["segment"],
        "local_seconds": r["local_seconds"],
        "temperature": r["temperature"],
        "utc_time": r["utc_time"],
        "anchor_version": r["anchor_version"],
        "calc_version": r["calc_version"],
    }


def _compute(refs_db: list[dict], recs_db: list[dict],
             deviation_s: float, temp_limit: float) -> dict:
    """单次核验计算（不含版本判定）。"""
    refs = [{**r, "ts": r["utc_ts"]} for r in refs_db]

    window = None
    window_out = None
    if refs:
        lo = min(refs, key=lambda r: r["ts"])
        hi = max(refs, key=lambda r: r["ts"])
        window = (lo["ts"], hi["ts"])
        window_out = {"start": lo["utc_time"], "end": hi["utc_time"]}

    recs: list[dict] = []
    in_window: list[bool] = []
    out_of_window = 0
    for rec in recs_db:
        ts = round(parse_utc(rec["utc_time"]).timestamp(), 3)
        inw = window is not None and window[0] - _EPS <= ts <= window[1] + _EPS
        recs.append({**rec, "ts": ts})
        in_window.append(inw)
        if not inw:
            out_of_window += 1

    # 候选配对面向全体已发布读数（不被覆盖时段截断）
    m = match_points(refs, recs, deviation_s)

    pairs = []
    for ri, ci in m["pairs"]:
        ref, rec = refs[ri], recs[ci]
        diff = round(rec["temperature"] - ref["temperature"], 6)
        pairs.append({
            "ref": _pub_ref(ref),
            "rec": _pub_rec(rec),
            "temp_diff": diff,                       # 记录仪 − 参考仪
            "over_limit": abs(diff) > temp_limit,    # 限值闭区间：|Δ| ≤ 限值 为合格
            "time_delta_s": round(rec["ts"] - ref["ts"], 3),
        })
    pairs.sort(key=lambda p: (p["ref"]["utc_time"], p["ref"]["row_no"]))
    over_limit = [p for p in pairs if p["over_limit"]]

    conflicts = [
        {
            "refs": [_pub_ref(refs[i]) for i in sorted(c["refs"],
                     key=lambda i: (refs[i]["ts"], refs[i]["row_no"]))],
            "recs": [_pub_rec(recs[i]) for i in sorted(c["recs"],
                     key=lambda i: (recs[i]["ts"], recs[i]["row_no"]))],
        }
        for c in m["conflicts"]
    ]
    conflicts.sort(key=lambda c: (c["refs"][0]["utc_time"] if c["refs"]
                                  else c["recs"][0]["utc_time"]))

    missing_refs = [_pub_ref(refs[i]) for i in m["missing_refs"]]
    # 记录仪侧缺点：仅覆盖时段内的读数才计缺失；时段外读数不属于本次核对范围
    missing_recs = [_pub_rec(recs[i]) for i in m["missing_recs"] if in_window[i]]

    if not refs:
        verdict = "no_reference"
    elif conflicts or missing_refs or missing_recs or over_limit:
        verdict = "not_consistent"
    else:
        verdict = "consistent"   # 全部唯一配对且无超限（有参考点时必有配对）

    return {
        "verdict": verdict,
        "window": window_out,
        "counts": {
            "refs_total": len(refs),
            "recorders_in_window": sum(in_window),
            "recorders_out_of_window": out_of_window,
            "pairs": len(pairs),
            "over_limit": len(over_limit),
            "conflict_clusters": len(conflicts),
            "conflict_refs": sum(len(c["refs"]) for c in conflicts),
            "conflict_recs": sum(len(c["recs"]) for c in conflicts),
            "missing_refs": len(missing_refs),
            "missing_recs": len(missing_recs),
        },
        "pairs": pairs,
        "over_limit": over_limit,
        "conflicts": conflicts,
        "missing": {"reference_side": missing_recs, "recorder_side": missing_refs},
    }


def run_verification(db: Database, deviation_s: float, temp_limit: float,
                     *, retries: int = MAX_VERIFY_RETRIES, stamp_fn=None) -> dict:
    """执行一次温度核对。

    核验基于同一完整发布版：前后版本戳一致才接受结果；期间换版则重试，
    重试耗尽仍不稳定 → 旧版回看。数据代次未追平（dirty）→ 旧版回看。
    ``stamp_fn`` 仅供测试注入版本戳序列。
    """
    stamp_fn = stamp_fn or (lambda: _version_stamp(db))
    stable = False
    attempts = 0
    for _ in range(retries + 1):
        attempts += 1
        pre = stamp_fn()
        refs, recs = _load_inputs(db)
        # 防御：同一快照内校时版本必须唯一（原子换版保证，异常时视为换版）
        mixed = len({r["calc_version"] for r in recs}) > 1
        result = _compute(refs, recs, deviation_s, temp_limit)
        post = stamp_fn()
        if not mixed and pre == post:
            stable = True
            break

    calc_version, state_gen, calibrated_gen = post
    stale_reasons = []
    if not stable:
        stale_reasons.append(f"核验期间发布版发生变化（{attempts} 次尝试均不稳定）")
    if state_gen != calibrated_gen:
        stale_reasons.append("校时结果未追平最新数据（存在待重算的上传或锚点更正）")
    currency = "stale" if stale_reasons else "current"

    result.update({
        "params": {"deviation_s": deviation_s, "temp_limit": temp_limit},
        "calc_version": calc_version,
        "state_generation": state_gen,
        "calibrated_generation": calibrated_gen,
        "attempts": attempts,
        "currency": currency,
        "stale_reasons": stale_reasons,
        # 只有「当前版 + 全部唯一配对 + 无超限」才算当前合格；旧版回看绝不合格
        "qualified": result["verdict"] == "consistent" and currency == "current",
    })
    return result
