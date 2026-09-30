"""箱体修洗接口：维护修洗任务，覆盖安排修洗、开始修理、验收完成等动作。

所有写操作都要求带操作人（姓名、所属堆场、角色）与 request_id：
* 越权（角色缺失、跨堆场、只读岗）由服务层当场驳回并写明缺的角色；
* request_id 保证重复提交只算一次，expected_version 做乐观并发控制。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, PageResult, RepairCommand
from app.services.repair import RepairService

router = APIRouter(prefix="/api/repair", tags=["箱体修洗"])

service = RepairService()

LIST_FIELDS = ["任务编号", "箱号", "所属堆场", "损伤类型", "修理等级", "修理人员", "清洗方式", "验收人员", "修洗状态"]
STATUSES = ["待修洗", "修理中", "待验收", "已完成"]


def _to_result(result: dict[str, Any]) -> ActionResult:
    return ActionResult(
        ok=result["ok"],
        message=result["message"],
        entry=result.get("entry"),
        deduplicated=bool(result.get("deduplicated")),
    )


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
def repair_stats() -> dict[str, int]:
    """修洗看板：各状态数量全部从明细实时重算，待验收数与列表永远对得齐。"""
    return service.stats()


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出箱体修洗清单：返回当前全量数据（与列表同源）。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "repair", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条修洗任务明细；不存在时给出可读的错误说明。

    列表与明细共用同一份仓储数据，验收人在两处读到的值保持一致。
    """
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"修洗任务 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: RepairCommand) -> ActionResult:
    """登记一条修洗任务；缺字段、任务编号重复或越权时都说明原因而不是静默多一条。"""
    result = service.create_entry(
        payload.values,
        actor=payload.actor.model_dump(),
        request_id=payload.request_id,
    )
    return _to_result(result)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: RepairCommand) -> ActionResult:
    """对单条任务执行安排修洗、开始修理、验收完成。

    越权、只读、状态倒流、重复验收、并发抢同一条都会被服务层当场驳回，
    驳回信息里写明缺的角色或冲突原因。
    """
    result = service.run_action(
        entry_id,
        str(payload.action or payload.values.get("action") or "").strip(),
        actor=payload.actor.model_dump(),
        request_id=payload.request_id,
        expected_version=payload.expected_version,
    )
    return _to_result(result)


@router.patch("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: RepairCommand) -> ActionResult:
    """修改修洗明细字段；只读岗改损伤类型、修理等级会被当场驳回。"""
    result = service.update_entry(
        entry_id,
        payload.values,
        actor=payload.actor.model_dump(),
        request_id=payload.request_id,
        expected_version=payload.expected_version,
    )
    return _to_result(result)
