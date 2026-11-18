"""网页采集规则接口：维护采集规则，覆盖选择器测试、发布快照与运行引用。

路由分层：

* ``/api/crawler/snapshots`` 采集规则发布快照的只读视图与生命周期动作；
* ``/api/crawler/tests`` 选择器测试，测试结果与规则版本、选择器同版；
* ``/api/crawler/runs`` 生产采集运行，每次运行只引用一个已发布快照；
* ``/api/crawler`` 规则草稿的登记、筛选与原有状态动作。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, PageResult
from app.services.crawler import CrawlerService

router = APIRouter(prefix="/api/crawler", tags=["网页采集规则"])

service = CrawlerService()

LIST_FIELDS = ["规则编号", "目标网址", "页面选择器", "正文选择器", "翻页参数", "抓取频率", "字符编码", "去重策略"]
STATUSES = ["草稿", "测试中", "已发布", "已停用"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


def _action_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    return {key: value for key, value in (payload or {}).items() if key not in {"action", "remark"}}


# --------------------------------------------------------------- 快照优先声明
@router.get("/snapshots", response_model=dict)
def list_snapshots(
    rule_id: int | None = Query(default=None, description="按采集规则过滤"),
    status: str | None = Query(default=None, description="草稿、待发布、待生效、已发布、已停用、已归档"),
    include_archived: bool = Query(default=True),
) -> dict:
    """规则页快照抽屉：一次看到草稿、待发布与已发布快照。"""
    items = service.list_snapshots(rule_id=rule_id, status=status, include_archived=include_archived)
    return {"items": items, "total": len(items)}


@router.get("/snapshots/{snapshot_id}", response_model=dict)
def get_snapshot(snapshot_id: int) -> dict:
    snapshot = service.get_snapshot(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"采集规则快照 {snapshot_id} 不存在")
    return snapshot


@router.get("/snapshots/{snapshot_id}/audit", response_model=dict)
def get_snapshot_audit(snapshot_id: int) -> dict:
    records, message = service.get_audit(snapshot_id)
    if records is None:
        raise HTTPException(status_code=404, detail=message)
    return {"items": records, "total": len(records), "message": message}


@router.post("/snapshots/{snapshot_id}/actions", response_model=ActionResult)
def run_snapshot_action(snapshot_id: int, payload: dict[str, Any] | None = None) -> ActionResult:
    """快照动作：确认发布、归档、停用、重算摘要；同版本并发操作只有一次成功。"""
    body = payload or {}
    action = str(body.get("action") or "").strip()
    if not action:
        return ActionResult(ok=False, message="缺少快照动作 action")
    snapshot, message = service.run_snapshot_action(snapshot_id, action, body)
    return ActionResult(ok=snapshot is not None, message=message,
                        entry=None if snapshot is None else dict(snapshot))


# --------------------------------------------------------------- 选择器测试
@router.get("/tests", response_model=dict)
def list_tests(rule_id: int | None = Query(default=None)) -> dict:
    items = service.list_tests(rule_id)
    return {"items": items, "total": len(items)}


@router.get("/tests/{test_id}", response_model=dict)
def get_test(test_id: int) -> dict:
    test = service.get_test(test_id)
    if test is None:
        raise HTTPException(status_code=404, detail=f"选择器测试 {test_id} 不存在")
    return test


@router.post("/tests/{test_id}/complete", response_model=ActionResult)
def complete_test(test_id: int, payload: dict[str, Any] | None = None) -> ActionResult:
    """回填选择器测试结果；测试通过才生成同版本的待发布候选快照。"""
    body = payload or {}
    actor = str(body.get("操作人") or "值班管理员")
    test, message = service.complete_test(test_id, body, actor)
    return ActionResult(ok=test is not None, message=message,
                        entry=None if test is None else dict(test))


@router.post("/parse", response_model=dict)
def parse_preview(payload: dict[str, Any] | None = None) -> dict:
    """解析目标网址、页面选择器与翻页参数，返回结构化结果与校验错误。"""
    body = payload or {}
    entry_id = body.get("规则ID")
    preview, message = service.parse_preview(body, int(entry_id) if entry_id else None)
    if preview is None:
        return {"ok": False, "message": message}
    return {"ok": preview["校验通过"], "message": message, "preview": preview}


@router.post("/{entry_id}/tests", response_model=ActionResult)
def submit_test(entry_id: int, payload: dict[str, Any] | None = None) -> ActionResult:
    """提交选择器测试：选择器、翻页参数、规则版本与测试结果同版落库。

    ``自动测试`` 缺省为开启，便于运营一次提交即得到候选快照；需要人工回填
    测试结果时传 ``false``，随后走 ``/tests/{id}/complete``。
    """
    body = payload or {}
    actor = str(body.get("操作人") or "值班管理员")
    auto_run = body.get("自动测试", True) not in (False, "false", "False", 0)
    entry, message = service.submit_test(entry_id, _action_payload(body), actor, auto_run=auto_run)
    return ActionResult(ok=entry is not None, message=message,
                        entry=None if entry is None else dict(entry))


@router.get("/{entry_id}/effective", response_model=dict)
def get_effective_snapshot(entry_id: int) -> dict:
    """生产采集实际读取的唯一规则版本。"""
    snapshot, message = service.effective_snapshot(entry_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=message)
    return snapshot


# ------------------------------------------------------------------- 运行任务
@router.get("/runs", response_model=dict)
def list_runs(
    rule_id: int | None = Query(default=None),
    snapshot_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
) -> dict:
    items = service.list_runs(rule_id=rule_id, snapshot_id=snapshot_id, status=status)
    return {"items": items, "total": len(items)}


@router.post("/runs", response_model=ActionResult)
def start_run(payload: dict[str, Any] | None = None) -> ActionResult:
    """启动采集运行，只能引用一个已发布且测试通过的快照。"""
    run, message = service.start_run(payload or {})
    return ActionResult(ok=run is not None, message=message,
                        entry=None if run is None else dict(run))


@router.post("/runs/{run_id}/complete", response_model=ActionResult)
def complete_run(run_id: int, payload: dict[str, Any] | None = None) -> ActionResult:
    """结束运行；旧快照最后一个任务结束后，待生效快照自动推进为已发布。"""
    body = payload or {}
    actor = str(body.get("操作人") or "调度器")
    run, message = service.complete_run(run_id, body, actor)
    return ActionResult(ok=run is not None, message=message,
                        entry=None if run is None else dict(run))


@router.get("/{entry_id}/draft", response_model=dict)
def get_draft(entry_id: int) -> dict:
    """规则页草稿视图：草稿字段与生产实际读取的版本分开呈现。"""
    view = service.draft_view(entry_id)
    if view is None:
        raise HTTPException(status_code=404, detail=f"采集规则 {entry_id} 不存在或已归档")
    return view


@router.post("/{entry_id}/draft", response_model=ActionResult)
def update_draft(entry_id: int, payload: dict[str, Any] | None = None) -> ActionResult:
    """修改规则草稿；已发布快照不被覆盖，生产采集继续读旧版本。"""
    entry, message = service.update_draft(entry_id, payload or {})
    return ActionResult(ok=entry is not None, message=message,
                        entry=None if entry is None else dict(entry))


# ----------------------------------------------------------------- 规则列表
@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、测试中、已发布、已停用"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、规则编号关键字与状态过滤网页采集规则列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("﻿")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status", "规则版本", "生效版本"])
    for item in items:
        row = []
        values = [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS),
                  item.get("status", ""), item.get("规则版本", ""), item.get("生效版本", "")]
        for value in values:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="crawler.csv"'})


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条采集规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"采集规则 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条采集规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="采集规则已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条采集规则执行提交测试、发布规则、停用规则。

    提交测试会把选择器测试与规则版本绑定；发布规则只在候选快照通过测试且被
    显式确认后生效，不允许测试中的候选规则直接覆盖生产采集。
    """
    action = payload.action.strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
