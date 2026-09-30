"""箱体修洗业务规则：堆场归属、角色鉴权、单向状态流转、幂等与并发控制都收在这里。

两类毛病（越权替别的堆场验收、两班组并发点验收）同一根因：状态流转没有任何
归属、角色、顺序与并发校验。本模块把验收环节的每一次流转都收紧：

* 验收只能由本堆场、持「修洗验收员」角色的账号执行；只读角色一律从严驳回；
* 状态只能沿 待修洗→修理中→待验收→已完成 单向推进，已验收不可回退；
* request_id 幂等账本保证同一条重复提交只算一次；
* version 乐观锁 + 进程内互斥锁保证并发提交只有一方生效，另一方当场收到驳回；
* 验收成功时把验收人快照写进任务，交接（换人登录）后仍以原验收人为准。
"""
from __future__ import annotations

import copy
import threading
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "repair"
REQUIRED_FIELDS = ["任务编号", "箱号", "损伤类型", "所属堆场"]
STATUS_ORDER = ["待修洗", "修理中", "待验收", "已完成"]
ACTION_RULES = {"安排修洗": "修理中", "开始修理": "待验收", "验收完成": "已完成"}
ACCEPT_ACTION = "验收完成"
DONE_STATUS = STATUS_ORDER[-1]
WAIT_ACCEPT_STATUS = "待验收"

ROLE_ACCEPTOR = "修洗验收员"
ROLE_READONLY = "只读员"

# 允许在编辑接口修改的字段；其中受保护字段只读岗无权改动
EDITABLE_FIELDS = ("箱号", "损伤类型", "修理等级", "修理人员", "清洗方式")
PROTECTED_FIELDS = ("损伤类型", "修理等级")
IMMUTABLE_NOTE = "状态、所属堆场、验收人员只能由验收环节写入，不能直接编辑"

# 流转结果：ok=是否生效，deduplicated=是否命中幂等账本（只算首次）
Result = dict[str, Any]


def _fail(message: str) -> Result:
    return {"ok": False, "message": message, "entry": None, "deduplicated": False}


class RepairService:
    def __init__(self) -> None:
        # 修洗模块的所有写操作在同一把锁内完成“校验—落账”，并发请求被串行化
        self._lock = threading.RLock()
        # request_id -> 首次提交的结果快照，用于幂等重放
        self._idempotency: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ 读取
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

    def stats(self) -> dict[str, int]:
        """看板指标一律从明细实时重算，不允许与列表对不上。"""
        counters = {status: 0 for status in STATUS_ORDER}
        for row in store.rows(MODULE):
            status = str(row.get("status") or STATUS_ORDER[0])
            counters[status] = counters.get(status, 0) + 1
        counters["total"] = len(store.rows(MODULE))
        return counters

    # ------------------------------------------------------------------ 写入
    def create_entry(
        self,
        values: dict[str, Any],
        *,
        actor: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> Result:
        actor = self._normalize_actor(actor)
        with self._lock:
            replayed = self._replay(request_id)
            if replayed is not None:
                return replayed

            identity = self._identity_problems(actor)
            if identity:
                return _fail(f"鉴权失败：{'、'.join(identity)}，不能登记修洗任务")
            if ROLE_READONLY in actor["roles"]:
                return _fail(f"账号含「{ROLE_READONLY}」角色，不能登记修洗任务（只读与业务角色冲突时以更严的只读为准）")

            missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
            if missing:
                return _fail(f"缺少必填字段：{'、'.join(missing)}")

            task_no = str(values["任务编号"]).strip()
            existing = next((row for row in store.rows(MODULE) if row.get("任务编号") == task_no), None)
            if existing is not None:
                # 重复建单只认第一条，绝不在列表里再多出一条
                return {
                    "ok": True,
                    "message": f"任务编号 {task_no} 已登记（序号 {existing['id']}），不重复建单",
                    "entry": copy.deepcopy(existing),
                    "deduplicated": True,
                }

            rows = store.rows(MODULE)
            entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            for field in ("任务编号", "箱号", "损伤类型", "修理等级", "修理人员", "清洗方式", "所属堆场"):
                entry[field] = str(values.get(field) or "").strip()
            entry.update({
                "status": STATUS_ORDER[0],
                "修洗状态": STATUS_ORDER[0],
                "pending": False,
                "abnormal": False,
                "version": 1,
                "验收人员": "",
                "验收堆场": "",
                "验收时间": None,
            })
            rows.append(entry)
            return self._commit(request_id, True, "修洗任务已登记", entry, action="登记")

    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        actor: dict[str, Any] | None = None,
        request_id: str | None = None,
        expected_version: int | None = None,
    ) -> Result:
        action = str(action or "").strip()
        actor = self._normalize_actor(actor)
        with self._lock:
            replayed = self._replay(request_id, entry_id=entry_id, action=action)
            if replayed is not None:
                return replayed

            entry = store.find(MODULE, entry_id)
            if entry is None:
                return _fail(f"修洗任务 {entry_id} 不存在或已归档")
            if action not in ACTION_RULES:
                return _fail(f"动作「{action}」不属于箱体修洗可执行范围")

            identity = self._identity_problems(actor)
            if identity:
                return _fail(f"鉴权失败：{'、'.join(identity)}，不能执行「{action}」")

            current = str(entry.get("status") or "")
            # 1) 归属与角色：越权当场驳回，并写明缺的是什么
            denials: list[str] = []
            if ROLE_READONLY in actor["roles"]:
                denials.append(
                    f"账号含「{ROLE_READONLY}」角色，不能执行「{action}」"
                    "（只读与业务角色冲突时以更严的只读为准）"
                )
            if action == ACCEPT_ACTION:
                if ROLE_ACCEPTOR not in actor["roles"]:
                    denials.append(f"越权驳回：缺少角色「{ROLE_ACCEPTOR}」，无权执行验收")
                owner_yard = str(entry.get("所属堆场") or "")
                if actor["yard"] != owner_yard:
                    denials.append(
                        f"越权驳回：该任务归属「{owner_yard}」，验收只能由本堆场执行，"
                        f"当前账号属于「{actor['yard']}」"
                    )
            if denials:
                return _fail("；".join(denials))

            # 2) 乐观锁：别人已经改过这条，手里的旧版本必须先刷新。
            #    两班组并发点同一条时，落败方在这里被驳回而不是界面假成功。
            if expected_version is not None and int(entry.get("version", 1)) != int(expected_version):
                return _fail(
                    f"该任务已被他人更新（当前版本 v{entry.get('version', 1)}），"
                    "列表已刷新，请以最新数据重试"
                )

            # 3) 终态保护：已完成的任务既不能重复验收，也不能被推回待验收
            if current == DONE_STATUS:
                acceptor = entry.get("验收人员") or "—"
                return _fail(f"任务已是「已完成」，验收人：{acceptor}，不能重复验收或回退到「待验收」")

            # 4) 单向流转：当前状态只允许走向紧邻的下一状态
            target = ACTION_RULES[action]
            try:
                current_index = STATUS_ORDER.index(current)
            except ValueError:
                return _fail(f"当前状态「{current}」不在允许的状态序列里")
            next_status = STATUS_ORDER[current_index + 1] if current_index + 1 < len(STATUS_ORDER) else None
            if next_status != target:
                edge_action = next(name for name, to in ACTION_RULES.items() if to == next_status)
                return _fail(f"当前状态「{current}」只能执行「{edge_action}」，不能直接「{action}」；状态只能向前流转")

            # 5) 落账：验收时固化验收人快照，之后换人交接也不改写
            entry["status"] = target
            entry["修洗状态"] = target
            entry["pending"] = target == WAIT_ACCEPT_STATUS
            entry["abnormal"] = False
            entry["version"] = int(entry.get("version", 1)) + 1
            message = f"修洗任务已{action}"
            if action == ACCEPT_ACTION:
                entry["验收人员"] = actor["name"]
                entry["验收堆场"] = actor["yard"]
                entry["验收时间"] = datetime.now().isoformat(timespec="seconds")
                message += f"，验收人：{actor['name']}（{actor['yard']}）"
            return self._commit(request_id, True, message, entry, action=action)

    def update_entry(
        self,
        entry_id: int,
        values: dict[str, Any],
        *,
        actor: dict[str, Any] | None = None,
        request_id: str | None = None,
        expected_version: int | None = None,
    ) -> Result:
        actor = self._normalize_actor(actor)
        with self._lock:
            replayed = self._replay(request_id, entry_id=entry_id, action="编辑")
            if replayed is not None:
                return replayed

            entry = store.find(MODULE, entry_id)
            if entry is None:
                return _fail(f"修洗任务 {entry_id} 不存在或已归档")

            identity = self._identity_problems(actor)
            if identity:
                return _fail(f"鉴权失败：{'、'.join(identity)}，不能修改修洗任务")

            changes = {key: value for key, value in values.items() if str(value or "").strip() != ""}
            forbidden = [key for key in changes if key not in EDITABLE_FIELDS]
            if forbidden:
                return _fail(f"「{'、'.join(forbidden)}」不允许直接修改：{IMMUTABLE_NOTE}")
            if not changes:
                return _fail("没有需要更新的字段")

            # 只读岗：对受保护字段的修改当场驳回（与任何业务角色冲突都从严）
            if ROLE_READONLY in actor["roles"]:
                protected = [key for key in changes if key in PROTECTED_FIELDS]
                if protected:
                    return _fail(f"只读岗不能修改「{'、'.join(protected)}」")
                return _fail("只读岗不能修改修洗任务数据（只读与业务角色冲突时以更严的只读为准）")

            # 归属与验收一致：明细也只能由本堆场的人改
            owner_yard = str(entry.get("所属堆场") or "")
            if actor["yard"] != owner_yard:
                return _fail(
                    f"越权驳回：该任务归属「{owner_yard}」，修洗明细只能由本堆场维护，"
                    f"当前账号属于「{actor['yard']}」"
                )

            if entry.get("status") == DONE_STATUS:
                return _fail("任务已验收完成，损伤类型与修理等级不能再改动")
            if expected_version is not None and int(entry.get("version", 1)) != int(expected_version):
                return _fail(
                    f"该任务已被他人更新（当前版本 v{entry.get('version', 1)}），"
                    "列表已刷新，请以最新数据重试"
                )

            entry.update(changes)
            entry["version"] = int(entry.get("version", 1)) + 1
            return self._commit(request_id, True, f"修洗任务 {entry_id} 已更新", entry, action="编辑")

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _normalize_actor(raw: dict[str, Any] | None) -> dict[str, Any]:
        raw = raw or {}
        roles = raw.get("roles") or []
        if isinstance(roles, str):
            roles = [roles]
        return {
            "name": str(raw.get("name") or "").strip(),
            "yard": str(raw.get("yard") or "").strip(),
            "roles": [str(role).strip() for role in roles if str(role).strip()],
        }

    @staticmethod
    def _identity_problems(actor: dict[str, Any]) -> list[str]:
        problems: list[str] = []
        if not actor["name"]:
            problems.append("缺少操作人姓名")
        if not actor["yard"]:
            problems.append("缺少所属堆场")
        if not actor["roles"]:
            problems.append("缺少角色信息")
        return problems

    def _replay(
        self,
        request_id: str | None,
        *,
        entry_id: int | None = None,
        action: str | None = None,
    ) -> Result | None:
        """命中幂等账本则原样重放首次结果；键被挪作他用则当场驳回。"""
        if not request_id:
            return None
        record = self._idempotency.get(request_id)
        if record is None:
            return None
        if entry_id is not None and (record["entry_id"] != entry_id or record["action"] != action):
            return _fail("request_id 已用于另一条任务或动作，拒绝复用，请重新提交")
        return {
            "ok": record["ok"],
            "message": record["message"],
            "entry": copy.deepcopy(record["entry"]),
            "deduplicated": True,
        }

    def _commit(
        self,
        request_id: str | None,
        ok: bool,
        message: str,
        entry: dict[str, Any],
        *,
        action: str,
    ) -> Result:
        snapshot = copy.deepcopy(entry)
        if request_id:
            self._idempotency[request_id] = {
                "entry_id": entry.get("id"),
                "action": action,
                "ok": ok,
                "message": message,
                "entry": snapshot,
            }
        return {"ok": ok, "message": message, "entry": snapshot, "deduplicated": False}
