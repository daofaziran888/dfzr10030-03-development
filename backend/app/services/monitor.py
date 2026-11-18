"""采集监控 分析看板服务：消费采集规则发布快照，按版本汇总并回写核对结果。

监控分析服务不再自己编造采集量，而是从网页采集规则模块的发布快照和采集运行
记录里取数：

* 每个发布快照的版本号对应一行监控口径，抓取条数与选择器错误都按版本汇总；
* 消费快照后把差异写回采集规则（``监控核对`` / ``核对结果``）；
* 已发布或已停用的版本进入“待办工作台”核对，同一待办只能被处理一次。
"""
from __future__ import annotations

import random
import threading
from typing import Any

from app.services.crawler import CrawlerService, compute_digest, now_text
from app.store import store

MODULE = "monitor"
TODO_MODULE = "monitor_todo"
REQUIRED_FIELDS = ["面板编号", "采集源总数", "今日采集量"]
STATUS_ORDER = ["正常", "预警", "告警"]
ACTION_RULES = {"查看详情": "正常"}
NEGATIVE_ACTIONS = []

TODO_STATUS_ORDER = ["待处理", "处理中", "已处理", "已忽略"]
TODO_ACTIONS = {"开始处理": "处理中", "标记已处理": "已处理", "忽略待办": "已忽略"}
CONSUMABLE_STATUSES = {"已发布", "已停用"}

crawler_service = CrawlerService()
_LOCK = threading.RLock()


def _signed(value: int) -> str:
    return f"+{value}" if value > 0 else str(value)


class MonitorService:
    # ------------------------------------------------------------ 版本口径取数
    def collect_snapshot_metrics(self) -> list[dict[str, Any]]:
        """按发布快照版本汇总抓取条数与选择器错误。"""
        crawler_service._ensure_ready()
        snapshots = [s for s in store.rows("crawler_snapshot") if s.get("status") in CONSUMABLE_STATUSES]
        snapshots.sort(key=lambda row: (int(row.get("规则ID", 0)), int(row.get("id", 0))))
        runs = store.rows("crawler_run")
        metrics: list[dict[str, Any]] = []
        for index, snapshot in enumerate(snapshots, start=1):
            snapshot_runs = [run for run in runs if int(run.get("快照ID", -1)) == int(snapshot.get("id", -1))]
            fetched = sum(int(run.get("抓取条数") or 0) for run in snapshot_runs)
            errors = sum(int(run.get("选择器错误") or 0) for run in snapshot_runs)
            run_count = len(snapshot_runs)
            succeeded = len([run for run in snapshot_runs if int(run.get("选择器错误") or 0) == 0])
            success_rate = round(succeeded / run_count, 3) if run_count else 0.0
            rule = store.find("crawler", int(snapshot.get("规则ID", -1)))
            baseline = ((rule or {}).get("监控核对") or {}).get("最近核对") or {}
            baseline_fetched = int(baseline.get("抓取条数") or 0)
            baseline_errors = int(baseline.get("选择器错误") or 0)
            fetched_diff = fetched - baseline_fetched
            error_diff = errors - baseline_errors
            if baseline:
                diff = f"抓取条数 {_signed(fetched_diff)}，选择器错误 {_signed(error_diff)}"
            else:
                diff = "首次核对，无历史基线"
            metrics.append({
                "id": index,
                "面板编号": snapshot.get("快照编号"),
                "规则ID": snapshot.get("规则ID"),
                "规则编号": snapshot.get("规则编号"),
                "快照ID": snapshot.get("id"),
                "版本号": snapshot.get("版本号"),
                "选择器版本": snapshot.get("版本号"),
                "快照状态": snapshot.get("status"),
                "目标网址": snapshot.get("目标网址"),
                "页面选择器": snapshot.get("页面选择器"),
                "抓取条数": fetched,
                "选择器错误": errors,
                "成功率": success_rate,
                "运行次数": run_count,
                "差异": diff,
                "抓取条数差": fetched_diff,
                "选择器错误差": error_diff,
                "摘要": snapshot.get("摘要"),
                "更新时间": max((run.get("结束时间") or run.get("开始时间") or "" for run in snapshot_runs),
                                default=snapshot.get("生效时间") or snapshot.get("创建时间") or now_text()),
                "status": "告警" if errors else "正常",
                "pending": bool(errors),
                "abnormal": bool(errors),
            })
        return metrics

    def list_results(self) -> list[dict[str, Any]]:
        return self.collect_snapshot_metrics()

    def get_summary(self) -> dict[str, Any]:
        rows = self.collect_snapshot_metrics()
        total_fetched = sum(int(row["抓取条数"]) for row in rows)
        total_errors = sum(int(row["选择器错误"]) for row in rows)
        runs = [run for run in store.rows("crawler_run") if run.get("status") == "已完成"]
        succeeded = len([run for run in runs if int(run.get("选择器错误") or 0) == 0])
        success_rate = round(succeeded / len(runs), 3) if runs else 0.0
        published = len([row for row in rows if row["快照状态"] == "已发布"])
        return {
            "metrics": [
                {"label": "已发布快照版本", "value": published, "trend": 0},
                {"label": "发布口径采集量", "value": total_fetched, "trend": 0},
                {"label": "选择器错误", "value": total_errors, "trend": 0},
                {"label": "任务成功率", "value": f"{round(success_rate * 100, 1)}%", "trend": 0},
            ],
            "versions": rows,
        }

    # ------------------------------------------------------------- 运行分析
    def run_analysis(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        rows = self.collect_snapshot_metrics()
        if rows:
            results: list[dict[str, Any]] = []
            for index, row in enumerate(rows, start=1):
                results.append({
                    "id": index,
                    "分组": row["版本号"],
                    "规则编号": row["规则编号"],
                    "版本号": row["版本号"],
                    "抓取条数": row["抓取条数"],
                    "选择器错误": row["选择器错误"],
                    "成功率": row["成功率"],
                    "差异": row["差异"],
                })
            return results
        count = int(params.get("样本范围", params.get("并行数量", "6")) or 6)
        results = []
        for i in range(count):
            results.append({
                "id": i + 1,
                "分组": f"分组{(i % 3) + 1}",
                "抓取条数": random.randint(60, 240),
                "选择器错误": random.randint(0, 3),
                "成功率": round(random.uniform(0.72, 0.99), 3),
                "差异": "暂无发布快照，使用模拟口径",
            })
        return results

    # -------------------------------------------------- 跨模块消费与回写
    def sync_from_crawler(self, actor: str | None = None) -> tuple[list[dict[str, Any]], str]:
        """消费采集规则发布快照：按版本取数，把差异回写采集规则并生成待办。

        同一快照重复消费只更新同一条待办；待办一旦处理结束不会被再次激活。
        """
        actor = actor or "值班管理员"
        with _LOCK:
            metrics = self.collect_snapshot_metrics()
            reviewed = 0
            for row in metrics:
                if not row.get("规则ID") or not row.get("快照ID"):
                    continue
                result = {
                    "抓取条数": row["抓取条数"],
                    "选择器错误": row["选择器错误"],
                    "差异": row["差异"],
                    "版本号": row["版本号"],
                }
                rule, _ = crawler_service.record_monitor_review(int(row["规则ID"]), int(row["快照ID"]), result)
                if rule is not None:
                    reviewed += 1
                self._upsert_todo(row, actor)
            message = f"已消费 {len(metrics)} 个发布快照版本，回写 {reviewed} 条采集规则核对结果"
            return metrics, message

    def _upsert_todo(self, row: dict[str, Any], actor: str) -> dict[str, Any]:
        todos = store.rows(TODO_MODULE)
        existing = [todo for todo in todos
                    if int(todo.get("快照ID", -1)) == int(row.get("快照ID", -1))
                    and todo.get("status") in {"待处理", "处理中"}]
        if existing:
            todo = existing[0]
            todo["核对差异"] = row["差异"]
            todo["抓取条数"] = row["抓取条数"]
            todo["选择器错误"] = row["选择器错误"]
            todo["更新时间"] = now_text()
            todo["abnormal"] = bool(row.get("选择器错误"))
            return todo
        todo = {
            "id": max((int(item.get("id", 0)) for item in todos), default=0) + 1,
            "status": "待处理",
            "pending": True,
            "abnormal": bool(row.get("选择器错误")),
            "待办编号": f"TODO-{row.get('版本号')}-{len(todos) + 1:04d}",
            "关联规则": row.get("规则编号"),
            "规则ID": row.get("规则ID"),
            "快照ID": row.get("快照ID"),
            "版本号": row.get("版本号"),
            "核对差异": row["差异"],
            "抓取条数": row["抓取条数"],
            "选择器错误": row["选择器错误"],
            "摘要": row.get("摘要") or compute_digest(row, str(row.get("版本号") or ""), "生效口径"),
            "来源": "采集监控",
            "操作人": actor,
            "创建时间": now_text(),
            "更新时间": now_text(),
            "处理人": None,
            "备注": "",
        }
        todos.append(todo)
        return todo

    def list_todos(self, status: str | None = None) -> list[dict[str, Any]]:
        rows = [dict(row) for row in store.rows(TODO_MODULE)]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        rows.sort(key=lambda row: int(row.get("id", 0)))
        return rows

    def run_todo_action(self, todo_id: int, action: str, actor: str | None = None,
                        remark: str = "") -> tuple[dict[str, Any] | None, str]:
        actor = actor or "值班管理员"
        with _LOCK:
            todo = store.find(TODO_MODULE, todo_id)
            if todo is None:
                return None, f"待办 {todo_id} 不存在"
            if action not in TODO_ACTIONS:
                return None, f"动作「{action}」不属于待办工作台可执行范围"
            target = TODO_ACTIONS[action]
            if todo.get("status") in {"已处理", "已忽略"}:
                return None, f"待办 {todo.get('待办编号')} 已结束，不能重复处理"
            if action == "开始处理" and todo.get("status") != "待处理":
                return None, f"待办 {todo.get('待办编号')} 当前状态为「{todo.get('status')}」，不能开始处理"
            if action != "开始处理" and todo.get("status") != "处理中":
                return None, f"待办 {todo.get('待办编号')} 需先开始处理"
            todo["status"] = target
            todo["pending"] = target == "处理中"
            todo["abnormal"] = target == "处理中" and bool(todo.get("选择器错误"))
            todo["处理人"] = actor
            todo["更新时间"] = now_text()
            todo["备注"] = remark or todo.get("备注") or ""
            return todo, f"待办 {todo.get('待办编号')} 已{action}"

    # ------------------------------------------------------------- 单条读取
    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)
