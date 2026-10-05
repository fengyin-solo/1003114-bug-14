"""历史皮带异常标记补正：把旧逻辑覆盖/清掉的标记按当时判定还原。

启动时执行一次：
- 能按动作流水或历史状态反推第一次真实判定的，补正 abnormal 并补落动作流水；
- 资料不足、无法还原的，不猜不改原标记，转在巷道巡检（roadway 待办）里留一条
  说明，等现场核实。说明带固定任务编号，重复执行不会产生重复待办。
"""
from __future__ import annotations

from app.services.belt import MODULE, reconcile_historical_entry
from app.store import store

ROADWAY_MODULE = "roadway"
NOTE_CODE_PREFIX = "ROAD-NOTE-BELT-"


def _ensure_roadway_note(belt_id: int, belt_code: str, reason: str) -> None:
    code = f"{NOTE_CODE_PREFIX}{belt_id}"
    rows = store.rows(ROADWAY_MODULE)
    if any(row.get("任务编号") == code for row in rows):
        return
    entry = {
        "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
        "任务编号": code,
        "维修巷道": "皮带异常标记待核实",
        "维修内容": f"皮带 {belt_code}（id={belt_id}）的历史异常标记无法自动补正：{reason}，请现场核实保护装置与皮带状态后补正。",
        "施工队伍": "待派发",
        "开工日期": "",
        "竣工日期": "",
        "验收人员": "",
        "任务状态": "待核实",
        "status": "待派发",
        "pending": True,
        "abnormal": False,
    }
    rows.append(entry)


def backfill_belt_abnormal_flags() -> int:
    """补正全部历史皮带，返回无法补正、已转巷道巡检待办的条数。"""
    unresolved = 0
    for entry in store.rows(MODULE):
        reason = reconcile_historical_entry(entry)
        if reason is not None:
            unresolved += 1
            _ensure_roadway_note(
                int(entry.get("id", 0)),
                str(entry.get("皮带编号", "")),
                reason,
            )
    return unresolved
