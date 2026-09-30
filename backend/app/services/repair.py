"""箱体修洗业务规则：状态流转、字段校验、验收归属与幂等都收在这里。

约定：
- 状态只能沿 STATUS_ORDER 单向前进，已验收的任务不能再被推回待验收；
- 验收按「所属堆场」划清归属，只有本堆场且持有验收角色的人能验收，
  越权当场驳回并写明缺的角色；只读岗与越权同时命中时，以更严的限制为准；
- 验收人在验收成功时一次性写入，之后交接班组、重复提交都不再改写，
  列表与明细读到的永远是同一份。
"""
from __future__ import annotations

import threading
import uuid
from typing import Any

from app.store import store

MODULE = "repair"
REQUIRED_FIELDS = ["任务编号", "箱号", "损伤类型", "所属堆场"]
STATUS_ORDER = ["待修洗", "修理中", "待验收", "已完成"]
DONE_STATUS = STATUS_ORDER[-1]
# 动作 -> (允许的来源状态, 目标状态)；来源不符一律驳回，杜绝逆向流转。
ACTION_RULES: dict[str, tuple[str, str]] = {
    "安排修洗": ("待修洗", "修理中"),
    "开始修理": ("修理中", "待验收"),
    "验收完成": ("待验收", "已完成"),
}
NEGATIVE_ACTIONS: list[str] = []

ACCEPT_ROLE = "修洗验收员"
READONLY_ROLE = "只读岗"
# 只读岗不得修改的判定字段；验收人、所属堆场任何接口都不允许外部直接改。
PROTECTED_FIELDS = ["损伤类型", "修理等级"]
SYSTEM_FIELDS = ["验收人员", "所属堆场"]

# 同一条任务的「读-判-写」串行化，两个班组同时点验收时只有一个能落账。
_action_lock = threading.RLock()


def is_pending(entry: dict[str, Any]) -> bool:
    """待处理口径统一由明细状态现算：未到「已完成」都算看板待处理。"""
    return entry.get("status") != DONE_STATUS


class RepairService:
    # ---- 查询 ----------------------------------------------------------------

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        yard: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if yard:
            rows = [row for row in rows if row.get("所属堆场") == yard]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def stats(self, *, yard: str | None = None) -> dict[str, int]:
        """看板待验收数等一律随明细现算，不读可能被写脏的缓存标志位。"""
        rows = store.rows(MODULE)
        if yard:
            rows = [row for row in rows if row.get("所属堆场") == yard]
        result = {status: 0 for status in STATUS_ORDER}
        for row in rows:
            status = row.get("status")
            if status in result:
                result[status] += 1
        return result

    # ---- 登记 ----------------------------------------------------------------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with _action_lock:
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["修理等级"] = values.get("修理等级")
            entry["修理人员"] = values.get("修理人员")
            entry["清洗方式"] = values.get("清洗方式")
            entry["验收人员"] = None
            entry["status"] = STATUS_ORDER[0]
            entry["abnormal"] = False
            self._sync_flags(entry)
            rows.append(entry)
        return entry, []

    # ---- 字段修改（只读岗受限） ----------------------------------------------

    def update_fields(
        self,
        entry_id: int,
        values: dict[str, Any],
        actor: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        actor = actor or {}
        with _action_lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"修洗任务 {entry_id} 不存在或已归档"
            if entry.get("status") == DONE_STATUS:
                return None, "任务已验收完成，损伤类型与修理等级已冻结，不能再修改"
            # 只读岗与其他身份冲突时以更严的为准：只要挂只读岗，改字段一律驳回。
            if READONLY_ROLE in actor.get("roles", []):
                blocked = [field for field in PROTECTED_FIELDS if field in values]
                if blocked:
                    return None, (
                        f"只读岗不得修改{'、'.join(blocked)}，"
                        f"请联系本堆场{ACCEPT_ROLE}处理"
                    )
            for field in SYSTEM_FIELDS:
                if field in values and str(values.get(field) or "") != str(entry.get(field) or ""):
                    return None, f"「{field}」由验收流程统一记录，不允许手工修改"
            for field in PROTECTED_FIELDS:
                if field in values:
                    entry[field] = values.get(field)
            return entry, "修洗任务字段已更新"

    # ---- 状态流转（验收归属在这里收口） --------------------------------------

    def run_action(
        self,
        entry_id: int,
        action: str,
        actor: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """返回 (任务, 说明, 是否幂等重放)。actor 携带当前操作人、堆场、角色。"""
        actor = actor or {}
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于箱体修洗可执行范围", False
        source, target = ACTION_RULES[action]

        with _action_lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"修洗任务 {entry_id} 不存在或已归档", False

            # 已验收任务再点验收：同一个人/同一次请求的重复提交只算一次（幂等成功），
            # 换了人来抢点则当场驳回，账上保留唯一验收人。
            if entry.get("status") == DONE_STATUS:
                if action == "验收完成" and self._is_same_acceptance(entry, actor, request_id):
                    return entry, f"任务已由{entry.get('验收人员')}验收完成，重复提交不再重复记账", True
                return None, (
                    f"任务已验收完成（验收人：{entry.get('验收人员') or '—'}），"
                    "不能重新验收或退回待验收"
                ), False

            if entry.get("status") != source:
                return None, (
                    f"任务当前为「{entry.get('status')}」，不能执行「{action}」；"
                    f"该动作只接受「{source}」状态"
                ), False

            # 验收动作按堆场收紧归属；越权与只读同时命中时先按更严的越权驳回。
            if action == "验收完成":
                denied = self._accept_denied(entry, actor)
                if denied:
                    return None, denied, False

            entry["status"] = target
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            if action == "验收完成":
                # 验收人只在首次成功时落账，之后任何人都改不动。
                entry["验收人员"] = actor.get("name") or "未知验收人"
                entry["验收堆场"] = actor.get("yard")
                entry["验收请求号"] = request_id or str(uuid.uuid4())
            self._sync_flags(entry)
            return entry, f"修洗任务已{action}", False

    # ---- 内部辅助 ------------------------------------------------------------

    def _accept_denied(self, entry: dict[str, Any], actor: dict[str, Any]) -> str | None:
        """返回驳回说明；放行时返回 None。说明里写明缺的是哪个角色/哪道归属。"""
        roles = actor.get("roles", [])
        own_yard = entry.get("所属堆场")
        actor_yard = actor.get("yard")
        if not actor_yard:
            return f"越权驳回：验收只能由本堆场（{own_yard}）的「{ACCEPT_ROLE}」执行，当前未归属任何堆场，缺少角色「{ACCEPT_ROLE}」"
        if actor_yard != own_yard:
            # 跨堆场即使用本堆场角色也不放行，归属优先。
            return (
                f"越权驳回：{actor_yard}无权验收{own_yard}的修洗任务，"
                f"验收只能由本堆场（{own_yard}）的「{ACCEPT_ROLE}」执行"
            )
        if ACCEPT_ROLE not in roles:
            return f"越权驳回：缺少角色「{ACCEPT_ROLE}」，仅本堆场（{own_yard}）验收员可执行验收"
        if READONLY_ROLE in roles:
            # 同名“验收员”但挂了只读岗：以更严的只读身份为准。
            return f"越权驳回：当前身份含「{READONLY_ROLE}」，只读岗不得执行验收，请由{ACCEPT_ROLE}办理"
        return None

    @staticmethod
    def _is_same_acceptance(
        entry: dict[str, Any], actor: dict[str, Any], request_id: str | None
    ) -> bool:
        accepted_by = entry.get("验收人员")
        if request_id and request_id == entry.get("验收请求号"):
            return True
        return bool(accepted_by) and actor.get("name") == accepted_by

    @staticmethod
    def _sync_flags(entry: dict[str, Any]) -> None:
        """看板标志位与明细状态保持同源，避免待验收数与列表对不上。"""
        entry["pending"] = is_pending(entry)
