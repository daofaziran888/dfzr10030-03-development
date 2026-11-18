"""采集监控 分析看板接口：按发布快照版本汇总、消费回写与待办工作台。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload
from app.services.monitor import MonitorService

router = APIRouter(prefix="/api/monitor", tags=["采集监控"])
service = MonitorService()
LIST_FIELDS = ["面板编号", "采集源总数", "今日采集量", "成功率", "平均延迟", "失败重试", "异常告警", "更新时间"]
STATUSES = ["正常", "预警", "告警"]


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


@router.get("/summary")
def get_summary() -> dict[str, Any]:
    """监控 KPI 与逐版本明细：成功量与选择器版本一一对应。"""
    return service.get_summary()


@router.get("/versions")
def list_versions() -> dict[str, Any]:
    """按发布快照版本汇总抓取条数和选择器错误。"""
    items = service.collect_snapshot_metrics()
    return {"items": items, "total": len(items)}


@router.post("/sync")
def sync_from_crawler(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """消费采集规则发布快照，并按版本把差异回写采集规则。"""
    actor = str((payload or {}).get("操作人") or "值班管理员")
    items, message = service.sync_from_crawler(actor)
    return {"ok": True, "message": message, "items": items, "total": len(items)}


@router.get("/todos")
def list_todos(status: str | None = Query(default=None)) -> dict[str, Any]:
    items = service.list_todos(status)
    return {"items": items, "total": len(items)}


@router.post("/todos/{todo_id}/actions", response_model=ActionResult)
def run_todo_action(todo_id: int, payload: ActionPayload) -> ActionResult:
    entry, message = service.run_todo_action(todo_id, payload.action.strip(), payload.remark)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("")
def list_results() -> dict[str, Any]:
    return {"items": service.list_results()}


@router.post("/run")
def run_analysis(payload: EntryPayload) -> dict[str, Any]:
    results = service.run_analysis(payload.values)
    return {"items": results}


@router.get("/export")
def export_entries() -> StreamingResponse:
    items = service.list_results()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    all_keys = ["id"]
    if items:
        all_keys = list(items[0].keys())
    writer.writerow(all_keys)
    for item in items:
        writer.writerow([csv_cell(item.get(k, "")) for k in all_keys])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=monitor.csv"})


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"监控面板 {entry_id} 不存在")
    return entry
