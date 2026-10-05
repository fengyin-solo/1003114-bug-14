"""皮带运输业务规则：状态流转、字段校验与筛选口径都收在这里。

异常标记纪律：
- 异常标记只允许由「真实判定」determine_abnormal 落笔，动作前后都不许清掉；
- 同一个动作重复提交不生效第二遍，标记只认第一次真实判定写入的值；
- 列表、详情、导出、运营概览共用同一套判定口径，不存在第二套读法；
- 历史数据在服务启动时按当时判定补正，补不了的在巷道维修待办里留一条说明。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "belt"
REQUIRED_FIELDS = ["皮带编号", "所属巷道", "运输长度"]
STATUS_ORDER = ["正常", "保护失效", "撕裂", "已修复"]
ACTION_RULES = {"登记失效": "保护失效", "停机修复": "撕裂", "复机确认": "已修复"}

# 真实判定口径：哪些状态算异常、各状态下保护装置怎么显示，全模块只认这一张表
ABNORMAL_STATUSES = {"保护失效", "撕裂"}
DEVICE_STATE_BY_STATUS = {"正常": "正常", "保护失效": "失效", "撕裂": "撕裂停机", "已修复": "正常"}
ACTION_BY_TARGET = {target: action for action, target in ACTION_RULES.items()}

# 补不了的历史数据到巷道维修（巷道巡检）待办里留说明
INSPECTION_MODULE = "roadway"
INTERNAL_FIELDS = ("applied_actions", "abnormal_backfilled")


def determine_abnormal(entry: dict[str, Any]) -> bool | None:
    """真实判定：按状态机落点认定是否异常；认不了的遗留状态返回 None，不猜、不写。"""
    status = str(entry.get("status") or "")
    if status not in STATUS_ORDER:
        return None
    return status in ABNORMAL_STATUSES


def overview_abnormal(entry: dict[str, Any]) -> bool:
    """运营概览用的判定：能认定的按真实判定重新算，认定不了的保留原标记，不许静默丢掉。"""
    verdict = determine_abnormal(entry)
    if verdict is None:
        return bool(entry.get("abnormal"))
    return verdict


def serialize(entry: dict[str, Any]) -> dict[str, Any]:
    """对外读口径：列表、详情、导出、动作回包都走这一个出口，内部记账字段不下发。"""
    return {key: value for key, value in entry.items() if key not in INTERNAL_FIELDS}


class BeltService:
    def __init__(self) -> None:
        store.register_abnormal_rule(MODULE, overview_abnormal)
        self.backfill_abnormal_markers()

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
        return [serialize(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return serialize(entry) if entry is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["applied_actions"] = []
        self._apply_determination(entry)
        rows.append(entry)
        return serialize(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"运输皮带 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于皮带运输可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        applied = entry.setdefault("applied_actions", [])
        if action in applied:
            # 重复提交不生效第二遍：状态与异常标记都保持第一次真实判定写入的值
            return serialize(entry), f"运输皮带已{action}，重复提交不再生效"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        applied.append(action)
        self._apply_determination(entry)
        return serialize(entry), f"运输皮带已{action}"

    def _apply_determination(self, entry: dict[str, Any]) -> bool:
        """真实判定落笔处：异常标记与保护装置口径只允许在这里写入。"""
        verdict = determine_abnormal(entry)
        if verdict is None:
            return False
        entry["abnormal"] = verdict
        entry["保护装置"] = DEVICE_STATE_BY_STATUS[str(entry["status"])]
        return True

    def backfill_abnormal_markers(self) -> dict[str, int]:
        """历史补正：按当时动作落点的判定重写异常标记；认不了的留巷道巡检待办。"""
        corrected = 0
        noted = 0
        for entry in store.rows(MODULE):
            if entry.get("abnormal_backfilled"):
                continue
            applied = entry.setdefault("applied_actions", [])
            last_action = ACTION_BY_TARGET.get(str(entry.get("status") or ""))
            if last_action is not None and last_action not in applied:
                applied.append(last_action)
            verdict = determine_abnormal(entry)
            if verdict is None:
                if self._leave_inspection_note(entry):
                    noted += 1
            else:
                if bool(entry.get("abnormal")) != verdict:
                    corrected += 1
                entry["abnormal"] = verdict
                entry["保护装置"] = DEVICE_STATE_BY_STATUS[str(entry["status"])]
            entry["abnormal_backfilled"] = True
        return {"corrected": corrected, "noted": noted}

    def _leave_inspection_note(self, entry: dict[str, Any]) -> bool:
        """补不了的标记在巷道维修待办里留一条说明；同一条皮带只留一次。"""
        rows = store.rows(INSPECTION_MODULE)
        code = f"BELT-FIX-{int(entry.get('id', 0)):04d}"
        if any(row.get("任务编号") == code for row in rows):
            return False
        rows.append({
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "status": "待派发",
            "pending": True,
            "abnormal": False,
            "任务编号": code,
            "维修巷道": str(entry.get("所属巷道") or "未知巷道"),
            "维修内容": (
                f"皮带 {entry.get('皮带编号', '?')} 的历史异常标记无法按当时判定补正"
                f"（当前状态「{entry.get('status', '?')}」不在认定序列内），"
                "标记保留原值，请人工现场核查后订正"
            ),
            "施工队伍": "待指派",
            "开工日期": "",
            "竣工日期": "",
            "验收人员": "待指派",
            "任务状态": "待派发",
        })
        return True
