"""运行配置：全部可通过环境变量覆盖。"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Settings:
    db_path: str = "data/app.db"          # SQLite 文件路径
    max_points: int = 2000                # 累计接纳读数上限
    recalc_delay: float = 0.0             # 重算提交前的人为延迟（秒），仅用于演示/测试并发窗口
    worker_enabled: bool = True           # 是否启用后台重算线程；关闭时变更后同步重算
    max_ref_points: int = 2000            # 参考点集点数上限（每次上传整体替换）
    ref_deviation_s: float = 30.0         # 默认允许对时偏差（秒），上传/核对时可覆盖
    ref_temp_limit: float = 0.5           # 默认温差限值（℃），上传/核对时可覆盖

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=os.environ.get("DB_PATH", "data/app.db"),
            max_points=int(os.environ.get("MAX_POINTS", "2000")),
            recalc_delay=float(os.environ.get("RECALC_DELAY", "0") or 0),
            worker_enabled=os.environ.get("WORKER_ENABLED", "1").strip().lower()
            not in ("0", "false", "no"),
            max_ref_points=int(os.environ.get("MAX_REF_POINTS", "2000")),
            ref_deviation_s=float(os.environ.get("REF_DEVIATION_S", "30") or 30),
            ref_temp_limit=float(os.environ.get("REF_TEMP_LIMIT", "0.5") or 0.5),
        )
