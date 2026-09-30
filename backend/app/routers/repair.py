"""箱体修洗接口：维护修洗任务，覆盖安排修洗、开始修理、验收完成等动作。

验收归属（本堆场 + 修洗验收员）与只读岗限制都在服务层收口，
路由层只负责把当前操作人的堆场、角色透传进去。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult, PageResult
from app.services.repair import (
    ACCEPT_ROLE,
    STATUS_ORDER,
    RepairService,
)

router = APIRouter(prefix="/api/repair", tags=["箱体修洗"])

service = RepairService()

LIST_FIELDS = ["任务编号", "箱号", "所属堆场", "损伤类型", "修理等级", "修理人员", "清洗方式", "验收人员", "修洗状态"]
STATUSES = STATUS_ORDER


class Actor(BaseModel):
    """当前操作人上下文：姓名、所属堆场、角色清单。缺省视为无角色的外堆场人员。"""

    name: str | None = None
    yard: str | None = None
    roles: list[str] = Field(default_factory=list)


class ActionPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None
    actor: Actor | None = None
    # 前端为同一次点击生成的幂等号；重复提交只算一次。
    request_id: str | None = None


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按任务编号检索"),
    status: str | None = Query(default=None, description="待修洗、修理中、待验收、已完成"),
    yard: str | None = Query(default=None, description="按所属堆场过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按任务编号、状态与堆场过滤箱体修洗列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, yard=yard, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/stats")
def repair_stats(yard: str | None = Query(default=None, description="按所属堆场统计")) -> dict[str, Any]:
    """修洗看板：待修洗/修理中/待验收/已完成数量随明细实时重算。"""
    counts = service.stats(yard=yard)
    return {"module": "repair", "yard": yard, "counts": counts, "pending_accept": counts.get("待验收", 0)}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出箱体修洗清单：返回全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "repair", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条修洗任务明细；不存在时给出可读的错误说明。验收人与列表同源。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"修洗任务 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: ActionPayload) -> ActionResult:
    """登记一条修洗任务，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="修洗任务已登记", entry=entry)


@router.patch("/{entry_id}", response_model=ActionResult)
def patch_entry(entry_id: int, payload: ActionPayload) -> ActionResult:
    """修改损伤类型、修理等级等判定字段；只读岗的修改会被当场驳回。"""
    actor = payload.actor.model_dump() if payload.actor else None
    entry, message = service.update_fields(entry_id, payload.values, actor)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条修洗任务执行安排修洗、开始修理、验收完成。

    - 状态只能沿既定顺序前进，已验收不能退回待验收；
    - 验收只接受本堆场持「{role}」角色的操作人，越权当场驳回并写明缺的角色；
    - 同一条重复提交只算一次；并发点击只有一次落账。
    """.format(role=ACCEPT_ROLE)
    action = str(payload.values.get("action") or "").strip()
    actor = payload.actor.model_dump() if payload.actor else None
    entry, message, replayed = service.run_action(entry_id, action, actor, payload.request_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
