"""皮带运输业务规则：状态流转、字段校验、筛选口径与异常标记都收在这里。

异常标记（abnormal）的规矩：
- 只能由「真实判定」动作写入：登记失效、停机修复是现场对保护失效/皮带撕裂的
  真实判定，写入 True；复机确认只是恢复生产的确认动作，不在判定表里，永不写标记。
- 标记只认第一次真实判定写入的值，首次写入即锁定，之后任何动作（动作前、动作后）
  都不许清掉或改写。
- 动作落库后才允许流转状态；同一个动作重复提交不产生第二条记录、不生效第二遍。
- is_abnormal 是异常标记的唯一读数口径，列表、详情、导出、运营概览都走它，
  不允许各入口另算一套。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "belt"
REQUIRED_FIELDS = ["皮带编号", "所属巷道", "运输长度"]
STATUS_ORDER = ["正常", "保护失效", "撕裂", "已修复"]
ACTION_RULES = {"登记失效": "保护失效", "停机修复": "撕裂", "复机确认": "已修复"}
# 只有真实判定动作会写异常标记；复机确认这类处置/恢复动作不在表里，永不写标记。
JUDGMENT_RESULTS: dict[str, bool] = {"登记失效": True, "停机修复": True}
# 动作流水的落库字段：每条记录保存动作、当时状态、判定结果（非判定动作为 None）、时刻。
ACTION_LOG = "actions"

# 历史数据补正时，按当前状态反推当时应已落库的动作链。
_STATUS_ACTION_CHAIN: dict[str, list[str]] = {
    "正常": [],
    "保护失效": ["登记失效"],
    "撕裂": ["登记失效", "停机修复"],
    "已修复": ["登记失效", "停机修复", "复机确认"],
}


def action_records(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """读取一条皮带已落库的动作流水；老数据没有流水时按空列表处理。"""
    records = entry.get(ACTION_LOG)
    return records if isinstance(records, list) else []


def is_abnormal(entry: dict[str, Any]) -> bool:
    """异常标记的唯一读数口径：取第一次真实判定写入的值，没有判定记 False。

    复机确认等非判定动作的 abnormal 为 None，在这里被跳过，因此任何恢复动作
    都不可能把标记清掉。
    """
    for record in action_records(entry):
        judged = record.get("abnormal")
        if judged is not None and record.get("action") in JUDGMENT_RESULTS:
            return bool(judged)
    return False


def present(entry: dict[str, Any]) -> dict[str, Any]:
    """列表、详情、导出共用的序列化口径：保护装置等业务字段原样读，异常标记
    统一按动作流水现算，保证任何入口读到的都是同一份结果。"""
    data = dict(entry)
    data[ACTION_LOG] = [dict(record) for record in action_records(entry)]
    data["abnormal"] = is_abnormal(entry)
    return data


def reconcile_historical_entry(entry: dict[str, Any]) -> str | None:
    """按当时的真实判定补正一条历史皮带的异常标记。

    能补正返回 None；无法据现有资料还原第一次判定的，返回原因说明，
    由调用方在巷道巡检待办里留一条说明，转人工现场核实。
    """
    records = action_records(entry)
    if records:
        # 有动作流水：流水即当时判定，按第一次真实判定锁定标记。
        unknown = [str(r.get("action")) for r in records if r.get("action") not in ACTION_RULES]
        if unknown:
            return f"动作流水里存在无法识别的动作（{'、'.join(unknown)}）"
        entry["abnormal"] = is_abnormal(entry)
        return None

    status = entry.get("status")
    chain = _STATUS_ACTION_CHAIN.get(status) if isinstance(status, str) else None
    if chain is None:
        return f"状态「{status}」不在允许的状态序列内，无法据状态反推当时判定"
    if not chain:
        if entry.get("abnormal"):
            return "皮带处于正常状态却带着异常标记，无法确认是否曾有真实判定"
        entry.setdefault(ACTION_LOG, [])
        entry["abnormal"] = False
        return None

    # 老数据没有动作流水：按状态链补落历史动作，再按第一次真实判定锁定标记。
    log = entry.setdefault(ACTION_LOG, [])
    for action in chain:
        log.append({
            "action": action,
            "status": ACTION_RULES[action],
            "abnormal": JUDGMENT_RESULTS.get(action),
            "at": None,
            "来源": "按历史状态补正",
        })
    entry["abnormal"] = is_abnormal(entry)
    return None


class BeltService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("皮带编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [present(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return present(entry) if entry is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry[ACTION_LOG] = []
        entry["abnormal"] = False
        rows.append(entry)
        return present(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"运输皮带 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于皮带运输可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        # 同一动作重复提交：不追加流水、不改状态、不动标记，第二遍一律不生效。
        if any(record.get("action") == action for record in action_records(entry)):
            return None, f"动作「{action}」已执行过，重复提交不再生效"

        # 先落动作流水，再流转状态；非判定动作 abnormal 记 None，不碰异常标记。
        log = entry.setdefault(ACTION_LOG, [])
        log.append({
            "action": action,
            "status": target,
            "abnormal": JUDGMENT_RESULTS.get(action),
            "at": datetime.now().isoformat(timespec="seconds"),
        })
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        # 标记只由流水里的第一次真实判定决定，动作前后都不会被清掉。
        entry["abnormal"] = is_abnormal(entry)
        return present(entry), f"运输皮带已{action}"
