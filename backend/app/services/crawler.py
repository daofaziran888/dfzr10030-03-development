"""网页采集规则业务规则：状态流转、字段校验与筛选口径都收在这里。

采集规则本身是运营编辑的“草稿”，真正被采集任务读取的是不可变的
``crawler_snapshot``（发布快照）。快照把目标网址、页面选择器、正文选择器、
翻页参数、规则版本和选择器测试结果绑在同一个版本里，保证生产采集在任一时刻
只读取一个完整、已测试通过的规则版本。

快照生命周期::

    草稿 ──测试通过──> 待发布 ──显式确认&旧快照无运行任务──> 已发布
                          │                                    │
                          └──旧快照仍有运行任务──> 待生效 ──旧任务结束自动切换──┘
    已发布 ──停用──> 已停用；任意阶段 ──归档──> 已归档（终态，不能回到运行阶段）

同一规则的快照操作统一走模块级可重入锁：同一对象并发确认发布时只有第一次
成功，余下会因状态或版本已变化被拒绝。
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlsplit

from app.store import store

MODULE = "crawler"
SNAPSHOT_MODULE = "crawler_snapshot"
TEST_MODULE = "crawler_test"
RUN_MODULE = "crawler_run"

LIST_FIELDS = ["规则编号", "目标网址", "页面选择器", "正文选择器", "翻页参数", "抓取频率", "字符编码", "去重策略"]
REQUIRED_FIELDS = ["规则编号", "目标网址", "页面选择器"]
STATUS_ORDER = ["草稿", "测试中", "已发布", "已停用"]
ACTION_RULES = {"提交测试": "测试中", "发布规则": "已发布", "停用规则": "已停用"}
NEGATIVE_ACTIONS = ["停用规则"]

# 快照绑定字段：选择器、翻页参数与测试结果必须来自同一个版本。
SNAPSHOT_FIELDS = ["目标网址", "页面选择器", "正文选择器", "翻页参数"]
# 生命周期：草稿 → 待发布 → (待生效) → 已发布 → 已停用 → 已归档。
SNAPSHOT_STATUS_ORDER = ["草稿", "待发布", "待生效", "已发布", "已停用", "已归档"]
CONFIRM_ACTIONS = {"确认发布", "确认并发布", "确认", "confirm-publish", "confirm_publish", "publish-confirm", "confirm"}
PUBLISH_ACTIONS = {"发布", "发布快照", "publish", "release"}
ARCHIVE_ACTIONS = {"归档", "归档快照", "archive"}
DISABLE_ACTIONS = {"停用", "停用快照", "disable"}
RECALCULATE_ACTIONS = {"重算摘要", "重算", "recalculate", "recalc"}
RUNNING_RUN_STATUSES = {"运行中", "执行中"}

_LOCK = threading.RLock()
_READY = False


def now_text() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _next_id(module: str) -> int:
    rows = store.rows(module)
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _canonical(values: dict[str, Any]) -> str:
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_digest(values: dict[str, Any], version: str, scope: str = "生效口径") -> str:
    """按生效口径计算稳定摘要；口径和版本都参与，旧摘要因此可追溯。"""
    payload = {
        "口径": scope,
        "规则版本": version,
        "字段": {field: values.get(field) or "" for field in SNAPSHOT_FIELDS},
        "测试结果": values.get("测试结果") or "未测试",
        "选择器摘要": values.get("选择器摘要") or "",
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


VERSION_PATTERN = re.compile(r"^[vV]?(\d+(?:\.\d+)*)$")


def version_tuple(version: Any) -> tuple[int, ...] | None:
    match = VERSION_PATTERN.match(str(version or "").strip())
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def normalize_version(version: Any) -> str:
    text = str(version or "").strip()
    if not text:
        return ""
    return text if text.lower().startswith("v") else f"v{text}"


def validate_version(version: Any, previous: Any = None) -> tuple[str, str]:
    """校验规则版本格式，并要求新版本严格大于上一个版本。"""
    normalized = normalize_version(version)
    parsed = version_tuple(normalized)
    if parsed is None:
        return "", f"规则版本「{version}」格式不合法，应为 v1、v2 或 1.2.0"
    if previous:
        previous_parsed = version_tuple(previous)
        if previous_parsed is not None and parsed <= previous_parsed:
            return "", f"规则版本「{normalized}」必须大于上一版本「{normalize_version(previous)}」"
    return normalized, ""


def parse_selector(raw: Any, label: str) -> tuple[str, list[str]]:
    """解析页面选择器：非空、括号配对、不含控制字符。"""
    text = str(raw or "").strip()
    errors: list[str] = []
    if not text:
        return "", [f"{label}不能为空"]
    if any(ord(char) < 32 for char in text):
        errors.append(f"{label}包含不可见控制字符")
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list[str] = []
    for char in text:
        if char in "([{":
            stack.append(char)
        elif char in pairs:
            if not stack or stack.pop() != pairs[char]:
                errors.append(f"{label}括号不配对")
                break
    if stack:
        errors.append(f"{label}括号不配对")
    return text, errors


def parse_target_url(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """解析目标网址：拆出协议、主机、路径与查询参数，草稿文本不强制是完整 URL。

    只要填了协议就必须有主机；没有协议的文本按草稿原样保留，不阻断登记。
    """
    text = str(raw or "").strip()
    if not text:
        return {}, ["目标网址不能为空"]
    if any(ord(char) < 32 for char in text):
        return {}, ["目标网址包含不可见控制字符"]
    parsed = urlsplit(text)
    info: dict[str, Any] = {
        "原始网址": text,
        "协议": parsed.scheme or "",
        "主机": parsed.netloc or "",
        "路径": parsed.path or "",
        "查询参数": {key: values[-1] if values else "" for key, values in parse_qs(parsed.query).items()},
    }
    if parsed.scheme and not parsed.netloc:
        return info, [f"目标网址「{text}」缺少主机名"]
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        return info, [f"目标网址协议「{parsed.scheme}」不在 http/https 范围内"]
    return info, []


def parse_pagination(raw: Any) -> tuple[dict[str, Any], str, list[str]]:
    """把翻页参数解析成结构化字典，同时给出规范化文本。

    兼容 JSON 对象、``page=1&size=20`` 查询串、``页参数=page;起始页=1``
    以及运营填写的自由文本。解析失败会返回可读错误，不会静默丢弃。
    """
    if isinstance(raw, dict):
        parsed = {str(key): value for key, value in raw.items()}
        return parsed, _canonical(parsed), []
    if isinstance(raw, (list, tuple)):
        return {"页码序列": list(raw)}, _canonical({"页码序列": list(raw)}), []
    text = str(raw or "").strip()
    if not text:
        return {}, "", []
    if text[0] in "{[":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as error:
            return {}, text, [f"翻页参数 JSON 解析失败：{error.msg}"]
        if not isinstance(parsed, (dict, list)):
            return {}, text, ["翻页参数必须是对象或数组"]
        if isinstance(parsed, list):
            parsed = {"页码序列": parsed}
        parsed = {str(key): value for key, value in parsed.items()}
        return parsed, _canonical(parsed), []
    if "=" in text:
        pairs = parse_qsl(text.replace(";", "&").replace("；", "&"), keep_blank_values=True)
        if not pairs:
            return {}, text, ["翻页参数没有可识别的键值对"]
        parsed = {key: value for key, value in pairs}
        return parsed, _canonical(parsed), []
    # 自由文本：保留原始值，允许草稿先登记，后续再结构化。
    parsed = {"原始参数": text}
    return parsed, _canonical(parsed), []


def _pagination_signature(raw: Any) -> str:
    """翻页参数统一按解析后的结构化口径比较，避免文本写法差异误判。"""
    if raw in (None, ""):
        return ""
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("{"):
            try:
                return _canonical(json.loads(text))
            except json.JSONDecodeError:
                return _canonical({"无法解析": text})
        if text.startswith("["):
            try:
                return _canonical({"页码序列": json.loads(text)})
            except json.JSONDecodeError:
                return _canonical({"无法解析": text})
    _, text, errors = parse_pagination(raw)
    return "" if errors else text


def _selector_summary(values: dict[str, Any]) -> str:
    material = "|".join(str(values.get(field) or "") for field in SNAPSHOT_FIELDS)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _validate_binding(values: dict[str, Any], version: str, test_result: dict[str, Any] | None) -> list[str]:
    """选择器、翻页参数和测试结果必须同版。"""
    errors: list[str] = []
    _, page_errors = parse_selector(values.get("页面选择器"), "页面选择器")
    errors.extend(page_errors)
    if str(values.get("正文选择器") or "").strip():
        _, body_errors = parse_selector(values.get("正文选择器"), "正文选择器")
        errors.extend(body_errors)
    _, _, pagination_errors = parse_pagination(values.get("翻页参数"))
    errors.extend(pagination_errors)
    _, url_errors = parse_target_url(values.get("目标网址"))
    errors.extend(url_errors)
    if test_result:
        test_version = normalize_version(test_result.get("版本号"))
        if test_version and test_version != version:
            errors.append(f"测试结果版本「{test_version}」与快照版本「{version}」不一致")
        test_url = str(test_result.get("目标网址") or "").strip()
        if test_url and test_url != str(values.get("目标网址") or "").strip():
            errors.append("测试结果的目标网址与快照不一致")
        for field in ("页面选择器", "正文选择器"):
            expected = str(test_result.get(field) or "").strip()
            actual = str(values.get(field) or "").strip()
            if expected and expected != actual:
                errors.append(f"测试结果的{field}与快照不一致")
        expected_pagination = _pagination_signature(test_result.get("翻页参数"))
        actual_pagination = _pagination_signature(values.get("翻页参数"))
        if expected_pagination and expected_pagination != actual_pagination:
            errors.append("测试结果的翻页参数与快照不一致")
    return errors


def _test_fields_pass(test_result: dict[str, Any] | None) -> bool:
    return bool(test_result) and str(test_result.get("结果") or "").strip() in {"通过", "已通过", "passed", "pass"}


def _append_audit(snapshot: dict[str, Any], action: str, actor: str, detail: str,
                  from_status: str | None = None, to_status: str | None = None,
                  digest_before: str | None = None, digest_after: str | None = None) -> None:
    snapshot.setdefault("审计记录", []).append({
        "时间": now_text(),
        "动作": action,
        "操作人": actor or "值班管理员",
        "原状态": from_status,
        "新状态": to_status,
        "说明": detail,
        "原摘要": digest_before,
        "新摘要": digest_after,
    })


class CrawlerService:
    # ------------------------------------------------------------------ 基础
    def _ensure_ready(self) -> None:
        """补齐历史数据上的版本字段与示例快照摘要。"""
        global _READY
        if _READY:
            return
        with _LOCK:
            if _READY:
                return
            for rule in store.rows(MODULE):
                rule.setdefault("规则版本", "v1")
                rule.setdefault("生效快照ID", None)
                rule.setdefault("待发布快照ID", None)
                rule.setdefault("待生效快照ID", None)
                rule.setdefault("生效版本", None)
            for snapshot in store.rows(SNAPSHOT_MODULE):
                snapshot.setdefault("测试结果", "通过" if snapshot.get("status") != "草稿" else "未测试")
                snapshot.setdefault("摘要口径", "生效口径")
                snapshot.setdefault("旧摘要", [])
                snapshot.setdefault("审计记录", [])
                if not snapshot.get("摘要"):
                    snapshot["摘要"] = compute_digest(
                        snapshot, snapshot.get("版本号", "v1"), snapshot.get("摘要口径", "生效口径"))
                if not snapshot.get("选择器摘要"):
                    snapshot["选择器摘要"] = _selector_summary(snapshot)
            self._sync_rule_pointers()
            _READY = True

    def _sync_rule_pointers(self) -> None:
        for rule in store.rows(MODULE):
            snapshots = [s for s in store.rows(SNAPSHOT_MODULE) if int(s.get("规则ID", -1)) == int(rule["id"])]
            published = [s for s in snapshots if s.get("status") == "已发布"]
            pending = [s for s in snapshots if s.get("status") == "待发布"]
            effective = [s for s in snapshots if s.get("status") == "待生效"]
            if published:
                rule["生效快照ID"] = published[-1]["id"]
                rule["生效版本"] = published[-1].get("版本号")
            else:
                rule["生效快照ID"] = None
                rule["生效版本"] = None
            rule["待发布快照ID"] = pending[-1]["id"] if pending else None
            rule["待生效快照ID"] = effective[-1]["id"] if effective else None

    def list_entries(self, *, filters: dict[str, str] | None = None, keyword: str | None = None,
                     status: str | None = None, page: int = 1, size: int = 20) -> tuple[list[dict[str, Any]], int]:
        self._ensure_ready()
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("规则编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        self._ensure_ready()
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        self._ensure_ready()
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with _LOCK:
            rows = store.rows(MODULE)
            entry = {"id": _next_id(MODULE)}
            entry.update({field: values.get(field) for field in LIST_FIELDS})
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            entry["规则版本"] = normalize_version(values.get("规则版本")) or "v1"
            entry["生效快照ID"] = None
            entry["生效版本"] = None
            entry["待发布快照ID"] = None
            entry["待生效快照ID"] = None
            rows.append(entry)
            return entry, []

    def update_draft(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """修改草稿：只影响后续测试/候选，不触碰已发布快照。"""
        self._ensure_ready()
        with _LOCK:
            rule = store.find(MODULE, entry_id)
            if rule is None:
                return None, f"采集规则 {entry_id} 不存在或已归档"
            if rule.get("status") == "已停用":
                return None, "已停用的采集规则不能修改草稿"
            changes = {field: values[field] for field in LIST_FIELDS if field in values}
            if changes:
                rule.update(changes)
                rule["草稿更新时间"] = now_text()
            return rule, "采集规则草稿已更新，已发布快照保持不变"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        self._ensure_ready()
        with _LOCK:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"采集规则 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于网页采集规则可执行范围"
            target = ACTION_RULES[action]
            if target not in STATUS_ORDER:
                return None, f"目标状态「{target}」不在允许的状态序列里"
            if action == "提交测试":
                # 提交测试即把当前草稿的选择器与翻页参数绑定到新版本，通过后
                # 生成待发布候选快照；候选快照不覆盖任何已发布版本。
                candidate = self._candidate_for_rule(entry_id)
                if candidate is None:
                    test, test_message = self.submit_test(entry_id, None, "值班管理员", auto_run=True)
                    if test is None:
                        return None, test_message
                entry["status"] = target
                entry["pending"] = target != STATUS_ORDER[-1]
                entry["abnormal"] = False
                return entry, f"采集规则已{action}，选择器测试与版本 {entry.get('规则版本')} 绑定"
            if action == "发布规则":
                candidate = self._candidate_for_rule(entry_id)
                if candidate is None:
                    return None, "发布动作必须等待候选快照被显式确认：请先完成选择器测试并生成待发布候选快照"
                if candidate.get("status") == "待生效":
                    return None, f"候选快照 {candidate.get('版本号')} 已确认，正等待旧快照运行任务结束"
                if candidate.get("status") != "待发布":
                    return None, f"候选快照 {candidate.get('版本号')} 当前状态为「{candidate.get('status')}」，不能再次确认发布"
                snapshot, message = self._confirm_publish_locked(candidate, {"操作人": action}, action)
                if snapshot is None:
                    return None, message
                entry["status"] = "已发布" if snapshot.get("status") == "已发布" else "测试中"
                entry["pending"] = snapshot.get("status") != "已发布"
                entry["abnormal"] = False
                return entry, message
            if action == "停用规则":
                published = self._current_published(entry_id)
                if published is not None and not self._has_running_runs(int(published["id"])):
                    snapshot, snapshot_message = self._disable_snapshot_locked(published, "值班管理员")
                    if snapshot is not None:
                        entry["status"] = target
                        entry["pending"] = target != STATUS_ORDER[-1]
                        entry["abnormal"] = True
                        return entry, f"采集规则已{action}；{snapshot_message}"
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            return entry, f"采集规则已{action}"

    def parse_preview(self, values: dict[str, Any] | None = None,
                      entry_id: int | None = None) -> tuple[dict[str, Any] | None, str]:
        """解析目标网址、页面选择器与翻页参数，只做预览不落库。"""
        self._ensure_ready()
        merged: dict[str, Any] = {field: (values or {}).get(field) for field in SNAPSHOT_FIELDS}
        if entry_id is not None:
            rule = store.find(MODULE, entry_id)
            if rule is None:
                return None, f"采集规则 {entry_id} 不存在或已归档"
            merged = self._resolve_rule_values(rule, values)
        url_info, url_errors = parse_target_url(merged.get("目标网址"))
        page_selector, page_errors = parse_selector(merged.get("页面选择器"), "页面选择器")
        body_selector, body_errors = parse_selector(merged.get("正文选择器"), "正文选择器") \
            if str(merged.get("正文选择器") or "").strip() else ("", [])
        pagination, pagination_text, pagination_errors = parse_pagination(merged.get("翻页参数"))
        errors = [*url_errors, *page_errors, *body_errors, *pagination_errors]
        preview = {
            "目标网址": url_info,
            "页面选择器": page_selector,
            "正文选择器": body_selector,
            "翻页参数": pagination,
            "翻页参数规范化": pagination_text,
            "校验通过": not errors,
            "错误": errors,
        }
        return preview, "解析完成" if not errors else "；".join(errors)

    def draft_view(self, entry_id: int) -> dict[str, Any] | None:
        """规则页草稿视图：草稿字段与生产实际读取的版本分开呈现。"""
        self._ensure_ready()
        rule = store.find(MODULE, entry_id)
        if rule is None:
            return None
        effective = self._current_published(entry_id)
        return {
            "规则": dict(rule),
            "草稿": {field: rule.get(field) for field in SNAPSHOT_FIELDS},
            "草稿版本": rule.get("规则版本"),
            "生效快照": None if effective is None else {
                "快照ID": effective.get("id"),
                "版本号": effective.get("版本号"),
                "摘要": effective.get("摘要"),
                "测试编号": effective.get("测试编号"),
            },
            "待发布快照ID": rule.get("待发布快照ID"),
            "待生效快照ID": rule.get("待生效快照ID"),
        }

    # -------------------------------------------------------------- 快照查询
    def list_snapshots(self, *, rule_id: int | None = None, status: str | None = None,
                       include_archived: bool = True) -> list[dict[str, Any]]:
        self._ensure_ready()
        rows = [dict(row) for row in store.rows(SNAPSHOT_MODULE)]
        if rule_id is not None:
            rows = [row for row in rows if int(row.get("规则ID", -1)) == int(rule_id)]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if not include_archived:
            rows = [row for row in rows if row.get("status") != "已归档"]
        rows.sort(key=lambda row: (int(row.get("规则ID", 0)), int(row.get("id", 0))))
        return rows

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        self._ensure_ready()
        return store.find(SNAPSHOT_MODULE, snapshot_id)

    def get_audit(self, snapshot_id: int) -> tuple[list[dict[str, Any]] | None, str]:
        snapshot = self.get_snapshot(snapshot_id)
        if snapshot is None:
            return None, f"采集规则快照 {snapshot_id} 不存在"
        return list(snapshot.get("审计记录", [])), "快照审计记录读取成功"

    def _candidate_for_rule(self, rule_id: int) -> dict[str, Any] | None:
        candidates = [s for s in store.rows(SNAPSHOT_MODULE)
                      if int(s.get("规则ID", -1)) == int(rule_id) and s.get("status") in {"待发布", "待生效"}]
        candidates.sort(key=lambda row: int(row.get("id", 0)))
        return candidates[-1] if candidates else None

    def _latest_snapshot(self, rule_id: int) -> dict[str, Any] | None:
        snapshots = [s for s in store.rows(SNAPSHOT_MODULE) if int(s.get("规则ID", -1)) == int(rule_id)]
        snapshots.sort(key=lambda row: int(row.get("id", 0)))
        return snapshots[-1] if snapshots else None

    def _next_version(self, rule_id: int) -> str:
        versions = [version_tuple(s.get("版本号")) for s in store.rows(SNAPSHOT_MODULE)
                    if int(s.get("规则ID", -1)) == int(rule_id)]
        versions = [value for value in versions if value]
        if not versions:
            return "v1"
        latest = max(versions)
        return f"v{latest[0] + 1}"

    # -------------------------------------------------------------- 选择器测试
    def _resolve_rule_values(self, rule: dict[str, Any], values: dict[str, Any] | None = None) -> dict[str, Any]:
        merged = {field: rule.get(field) for field in SNAPSHOT_FIELDS}
        for field in SNAPSHOT_FIELDS:
            if values and field in values and str(values.get(field) or "").strip():
                merged[field] = values.get(field)
        return merged

    def submit_test(self, rule_id: int, values: dict[str, Any] | None = None,
                    actor: str | None = None, auto_run: bool = True) -> tuple[dict[str, Any] | None, str]:
        """提交选择器测试：测试记录与规则版本、选择器、翻页参数同版落库。

        该动作不修改任何已发布快照；运营改了正文选择器，生产采集仍读旧快照。
        """
        self._ensure_ready()
        actor = actor or "值班管理员"
        with _LOCK:
            rule = store.find(MODULE, rule_id)
            if rule is None:
                return None, f"采集规则 {rule_id} 不存在或已归档"
            if rule.get("status") == "已停用":
                return None, "已停用的采集规则不能提交选择器测试"
            merged = self._resolve_rule_values(rule, values)
            latest = self._latest_snapshot(rule_id)
            requested = normalize_version(values.get("规则版本") if values else None) or normalize_version(rule.get("规则版本"))
            previous = latest.get("版本号") if latest else None
            if not requested:
                requested = self._next_version(rule_id)
            if previous and version_tuple(requested) and version_tuple(previous) and version_tuple(requested) <= version_tuple(previous):
                requested = self._next_version(rule_id)
            version, version_error = validate_version(requested, previous)
            if version_error:
                return None, version_error

            binding_errors = _validate_binding(merged, version, None)
            if binding_errors:
                return None, "；".join(binding_errors)

            _, pagination_text, _ = parse_pagination(merged.get("翻页参数"))
            test = {
                "id": _next_id(TEST_MODULE),
                "规则ID": rule_id,
                "规则编号": rule.get("规则编号"),
                "测试编号": f"TEST-{rule.get('规则编号') or rule_id}-{_next_id(TEST_MODULE):04d}",
                "版本号": version,
                "规则版本": version,
                "目标网址": merged.get("目标网址"),
                "页面选择器": merged.get("页面选择器"),
                "正文选择器": merged.get("正文选择器"),
                "翻页参数": pagination_text,
                "原始翻页参数": merged.get("翻页参数"),
                "结果": "测试中",
                "样本条数": 0,
                "抓取条数": 0,
                "选择器错误": 0,
                "耗时毫秒": 0,
                "测试人": actor,
                "测试时间": now_text(),
                "status": "测试中",
                "pending": True,
                "abnormal": False,
                "快照ID": None,
                "备注": "",
            }
            store.rows(TEST_MODULE).append(test)
            rule["测试编号"] = test["测试编号"]
            rule["规则版本"] = version
            if auto_run:
                return self._complete_test_locked(test, {"结果": "通过"}, actor)
            return test, f"选择器测试 {test['测试编号']} 已提交，等待测试结果"

    def complete_test(self, test_id: int, values: dict[str, Any] | None = None,
                      actor: str | None = None) -> tuple[dict[str, Any] | None, str]:
        self._ensure_ready()
        with _LOCK:
            test = store.find(TEST_MODULE, test_id)
            if test is None:
                return None, f"选择器测试 {test_id} 不存在"
            return self._complete_test_locked(test, values or {}, actor or "值班管理员")

    def _complete_test_locked(self, test: dict[str, Any], values: dict[str, Any],
                              actor: str) -> tuple[dict[str, Any] | None, str]:
        if test.get("status") != "测试中" or test.get("结果") != "测试中":
            return None, f"选择器测试 {test.get('测试编号')} 已结束，结果不可重复提交"
        if test.get("快照ID"):
            return None, f"选择器测试 {test.get('测试编号')} 已生成候选快照，不能再次推进"
        rule = store.find(MODULE, int(test.get("规则ID", -1)))
        if rule is None:
            return None, "测试关联的采集规则不存在或已归档"

        result = str(values.get("结果") or "通过").strip()
        passed = result in {"通过", "已通过", "passed", "pass"}
        selector_errors = int(values.get("选择器错误") or 0)
        fetched = int(values.get("抓取条数") or values.get("样本条数") or 0)
        if not passed and selector_errors <= 0:
            selector_errors = max(1, int(values.get("样本条数") or 1))

        test["结果"] = "通过" if passed else "失败"
        test["status"] = "已完成"
        test["pending"] = False
        test["abnormal"] = not passed
        test["选择器错误"] = selector_errors
        test["抓取条数"] = fetched
        test["样本条数"] = fetched
        test["耗时毫秒"] = int(values.get("耗时毫秒") or 128)
        test["测试时间"] = now_text()
        test["测试人"] = actor
        test["备注"] = str(values.get("备注") or "")
        rule["最近测试结果"] = test["结果"]
        rule["最近测试时间"] = test["测试时间"]

        if not passed:
            return test, f"选择器测试 {test['测试编号']} 未通过，未生成候选快照"

        version, version_error = validate_version(test.get("版本号"))
        if version_error:
            return None, version_error
        # 用测试提交时冻结的字段作为唯一来源，避免测试期间草稿被修改导致
        # 选择器、翻页参数和测试结果来自不同版本。
        merged = {field: test.get(field) for field in SNAPSHOT_FIELDS}
        binding_errors = _validate_binding(merged, version, test)
        if binding_errors:
            test["结果"] = "失败"
            test["abnormal"] = True
            return None, "；".join(binding_errors)
        active = [s for s in store.rows(SNAPSHOT_MODULE)
                  if int(s.get("规则ID", -1)) == int(test.get("规则ID", -1))
                  and s.get("status") in {"待发布", "待生效"}]
        if active:
            # 已有候选在等确认时，本次测试仍然通过、仍然可审计，但不再另起
            # 一个候选版本，避免同一规则出现两个待发布版本。
            test["快照ID"] = None
            test["快照版本"] = None
            test["重复候选"] = True
            return test, (f"选择器测试 {test['测试编号']} 通过；规则已有候选快照 "
                          f"{active[-1].get('版本号')}（{active[-1].get('status')}），本次结果仅留档")
        duplicate = [s for s in store.rows(SNAPSHOT_MODULE)
                     if int(s.get("规则ID", -1)) == int(test.get("规则ID", -1))
                     and normalize_version(s.get("版本号")) == version]
        if duplicate:
            test["结果"] = "失败"
            test["abnormal"] = True
            return None, f"版本 {version} 已存在发布快照，测试结果不能覆盖同版本快照"

        _, pagination_text, _ = parse_pagination(merged.get("翻页参数"))
        pagination = parse_pagination(merged.get("翻页参数"))[0]
        url_info = parse_target_url(merged.get("目标网址"))[0]
        snapshot = {
            "id": _next_id(SNAPSHOT_MODULE),
            "status": "待发布",
            "pending": True,
            "abnormal": False,
            "规则ID": int(test.get("规则ID", -1)),
            "规则编号": rule.get("规则编号"),
            "快照编号": f"SNAP-{rule.get('规则编号') or rule.get('id')}-{version}",
            "版本号": version,
            "规则版本": version,
            "目标网址": merged.get("目标网址"),
            "页面选择器": merged.get("页面选择器"),
            "正文选择器": merged.get("正文选择器"),
            "翻页参数": pagination_text,
            "翻页参数结构": pagination,
            "目标网址解析": url_info,
            "原始翻页参数": merged.get("翻页参数"),
            "测试编号": test.get("测试编号"),
            "测试ID": test.get("id"),
            "测试结果": "通过",
            "测试人": test.get("测试人"),
            "测试时间": test.get("测试时间"),
            "选择器摘要": _selector_summary(merged),
            "摘要口径": "生效口径",
            "旧摘要": [],
            "创建时间": now_text(),
            "发布时间": None,
            "生效时间": None,
            "归档时间": None,
            "基线快照ID": None,
            "审计记录": [],
            "监控核对": None,
        }
        snapshot["摘要"] = compute_digest(snapshot, version, "生效口径")
        snapshot["审计记录"] = []
        _append_audit(snapshot, "生成候选快照", actor, f"选择器测试 {test['测试编号']} 通过，生成待发布候选快照",
                      from_status=None, to_status="待发布", digest_after=snapshot["摘要"])
        store.rows(SNAPSHOT_MODULE).append(snapshot)
        test["快照ID"] = snapshot["id"]
        test["快照版本"] = version
        rule["待发布快照ID"] = snapshot["id"]
        self._sync_rule_pointers()
        self._write_rule_review(rule, snapshot, todo_note="选择器测试通过，等待发布确认")
        return snapshot, f"选择器测试通过，已生成待发布候选快照 {version}，发布前需显式确认"

    def list_tests(self, rule_id: int | None = None) -> list[dict[str, Any]]:
        self._ensure_ready()
        rows = [dict(row) for row in store.rows(TEST_MODULE)]
        if rule_id is not None:
            rows = [row for row in rows if int(row.get("规则ID", -1)) == int(rule_id)]
        rows.sort(key=lambda row: int(row.get("id", 0)))
        return rows

    def get_test(self, test_id: int) -> dict[str, Any] | None:
        self._ensure_ready()
        return store.find(TEST_MODULE, test_id)

    # -------------------------------------------------------------- 快照动作
    def run_snapshot_action(self, snapshot_id: int, action: str,
                            payload: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
        self._ensure_ready()
        payload = payload or {}
        actor = str(payload.get("操作人") or payload.get("actor") or "值班管理员")
        action = action.strip()
        with _LOCK:
            snapshot = store.find(SNAPSHOT_MODULE, snapshot_id)
            if snapshot is None:
                return None, f"采集规则快照 {snapshot_id} 不存在"
            if snapshot.get("status") == "已归档" and action not in RECALCULATE_ACTIONS:
                return None, f"快照 {snapshot.get('版本号')} 已归档，不能回到运行阶段"
            expected_version = normalize_version(payload.get("expected_version"))
            if expected_version and expected_version != normalize_version(snapshot.get("版本号")):
                return None, f"快照版本已变化：期望 {expected_version}，当前 {snapshot.get('版本号')}"
            expected_digest = str(payload.get("expected_digest") or "").strip()
            if expected_digest and expected_digest != snapshot.get("摘要"):
                return None, "快照摘要已变化，请刷新后重试"

            if action in CONFIRM_ACTIONS or action in PUBLISH_ACTIONS:
                if snapshot.get("status") == "待生效":
                    return None, f"快照 {snapshot.get('版本号')} 已确认，正在等待旧快照的运行任务结束"
                if snapshot.get("status") != "待发布":
                    return None, f"快照 {snapshot.get('版本号')} 当前状态为「{snapshot.get('status')}」，不能再次确认发布"
                return self._confirm_publish_locked(snapshot, payload, actor)
            if action in ARCHIVE_ACTIONS:
                return self._archive_snapshot_locked(snapshot, actor, str(payload.get("备注") or ""))
            if action in DISABLE_ACTIONS:
                return self._disable_snapshot_locked(snapshot, actor)
            if action in RECALCULATE_ACTIONS:
                return self._recalculate_snapshot_locked(snapshot, actor)
            if action in {"查看", "查看详情", "view"}:
                return snapshot, "快照详情读取成功"
            return None, f"动作「{action}」不属于采集规则快照可执行范围"

    def _test_for_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        test_id = snapshot.get("测试ID")
        if test_id:
            test = store.find(TEST_MODULE, int(test_id))
            if test is not None:
                return test
        test_no = snapshot.get("测试编号")
        if test_no:
            for test in store.rows(TEST_MODULE):
                if test.get("测试编号") == test_no:
                    return test
        return None

    def _binding_for_snapshot(self, snapshot: dict[str, Any]) -> list[str]:
        test = self._test_for_snapshot(snapshot)
        version, version_error = validate_version(snapshot.get("版本号"))
        if version_error:
            return [version_error]
        errors = _validate_binding(snapshot, version, test)
        if not _test_fields_pass(test):
            errors.append("快照没有同版本且通过的选择器测试结果")
        return errors

    def _has_running_runs(self, snapshot_id: int) -> bool:
        return any(run.get("status") in RUNNING_RUN_STATUSES for run in store.rows(RUN_MODULE)
                   if int(run.get("快照ID", -1)) == int(snapshot_id))

    def _current_published(self, rule_id: int) -> dict[str, Any] | None:
        published = [s for s in store.rows(SNAPSHOT_MODULE)
                     if int(s.get("规则ID", -1)) == int(rule_id) and s.get("status") == "已发布"]
        published.sort(key=lambda row: int(row.get("id", 0)))
        return published[-1] if published else None

    def _confirm_publish_locked(self, snapshot: dict[str, Any], payload: dict[str, Any],
                                actor: str) -> tuple[dict[str, Any] | None, str]:
        if snapshot.get("status") == "待生效":
            return None, f"快照 {snapshot.get('版本号')} 已确认，正在等待旧快照的运行任务结束"
        if snapshot.get("status") != "待发布":
            return None, f"快照 {snapshot.get('版本号')} 当前状态为「{snapshot.get('status')}」，不能再次确认发布"
        errors = self._binding_for_snapshot(snapshot)
        if errors:
            return None, "；".join(errors)
        rule_id = int(snapshot.get("规则ID", -1))
        rule = store.find(MODULE, rule_id)
        if rule is None:
            return None, "快照关联的采集规则不存在或已归档"
        if rule.get("status") == "已停用":
            return None, "采集规则已停用，候选快照不能确认发布"

        old_digest = snapshot.get("摘要")
        recalculated = compute_digest(snapshot, normalize_version(snapshot.get("版本号")), "生效口径")
        if old_digest and old_digest != recalculated:
            snapshot.setdefault("旧摘要", []).append({
                "摘要": old_digest, "口径": snapshot.get("摘要口径", "生效口径"), "记录时间": now_text(),
            })
        snapshot["摘要口径"] = "生效口径"
        snapshot["摘要"] = recalculated
        snapshot["选择器摘要"] = _selector_summary(snapshot)
        snapshot["确认人"] = actor
        snapshot["确认时间"] = now_text()

        baseline = self._current_published(rule_id)
        if baseline is not None and int(baseline.get("id", 0)) != int(snapshot.get("id", -1)) and self._has_running_runs(int(baseline["id"])):
            previous_status = snapshot.get("status")
            snapshot["status"] = "待生效"
            snapshot["pending"] = True
            snapshot["abnormal"] = False
            snapshot["基线快照ID"] = baseline["id"]
            snapshot["基线版本"] = baseline.get("版本号")
            rule["待生效快照ID"] = snapshot["id"]
            self._sync_rule_pointers()
            _append_audit(snapshot, "确认发布", actor,
                          f"旧发布快照 {baseline.get('版本号')} 仍有运行中任务，候选快照进入待生效",
                          from_status=previous_status, to_status="待生效",
                          digest_before=old_digest, digest_after=snapshot["摘要"])
            self._write_rule_review(rule, snapshot, todo_note="候选快照等待旧任务结束自动生效")
            return snapshot, f"候选快照 {snapshot.get('版本号')} 已确认，旧快照 {baseline.get('版本号')} 仍在运行，待当前任务结束后自动切换"

        return self._publish_snapshot_locked(snapshot, actor, old_digest, "显式确认发布")

    def _publish_snapshot_locked(self, snapshot: dict[str, Any], actor: str,
                                 old_digest: str | None, reason: str,
                                 audit_action: str = "发布生效") -> tuple[dict[str, Any] | None, str]:
        rule_id = int(snapshot.get("规则ID", -1))
        rule = store.find(MODULE, rule_id)
        if rule is None:
            return None, "快照关联的采集规则不存在或已归档"
        for other in store.rows(SNAPSHOT_MODULE):
            if (int(other.get("规则ID", -1)) == rule_id and other.get("status") == "已发布"
                    and int(other.get("id", 0)) != int(snapshot.get("id", -1))):
                other["status"] = "已停用"
                other["pending"] = False
                other["停用时间"] = now_text()
                _append_audit(other, "被新版本替换", actor,
                              f"快照 {snapshot.get('版本号')} 生效，旧快照停止承接新任务",
                              from_status="已发布", to_status="已停用")
        previous_status = snapshot.get("status")
        snapshot["status"] = "已发布"
        snapshot["pending"] = False
        snapshot["abnormal"] = False
        snapshot["发布时间"] = snapshot.get("发布时间") or now_text()
        snapshot["生效时间"] = now_text()
        snapshot["基线快照ID"] = None
        rule["status"] = "已发布"
        rule["pending"] = False
        rule["abnormal"] = False
        rule["生效快照ID"] = snapshot["id"]
        rule["生效版本"] = snapshot.get("版本号")
        rule["待发布快照ID"] = None
        rule["待生效快照ID"] = None
        self._sync_rule_pointers()
        _append_audit(snapshot, audit_action, actor, f"{reason}：生产采集切换到快照 {snapshot.get('版本号')}",
                      from_status=previous_status, to_status="已发布",
                      digest_before=old_digest, digest_after=snapshot.get("摘要"))
        self._write_rule_review(rule, snapshot, todo_note=f"快照 {snapshot.get('版本号')} 已发布生效")
        return snapshot, f"采集规则快照 {snapshot.get('版本号')} 已发布生效"

    def _archive_snapshot_locked(self, snapshot: dict[str, Any], actor: str,
                                 remark: str) -> tuple[dict[str, Any] | None, str]:
        if snapshot.get("status") == "已归档":
            return None, f"快照 {snapshot.get('版本号')} 已归档，不能重复归档"
        if snapshot.get("status") == "已发布":
            return None, "当前生效快照不能直接归档，请先发布新版本或停用"
        previous = snapshot.get("status")
        snapshot["status"] = "已归档"
        snapshot["pending"] = False
        snapshot["abnormal"] = False
        snapshot["归档时间"] = now_text()
        _append_audit(snapshot, "归档", actor, remark or "快照归档，进入只读历史",
                      from_status=previous, to_status="已归档")
        self._sync_rule_pointers()
        return snapshot, f"快照 {snapshot.get('版本号')} 已归档，归档后不能回到运行阶段"

    def _disable_snapshot_locked(self, snapshot: dict[str, Any], actor: str) -> tuple[dict[str, Any] | None, str]:
        if snapshot.get("status") != "已发布":
            return None, f"只有已发布快照可以停用，当前状态为「{snapshot.get('status')}」"
        if self._has_running_runs(int(snapshot.get("id", -1))):
            return None, "快照仍被运行中的采集任务引用，不能停用"
        snapshot["status"] = "已停用"
        snapshot["pending"] = False
        snapshot["停用时间"] = now_text()
        _append_audit(snapshot, "停用", actor, "快照停止承接新任务",
                      from_status="已发布", to_status="已停用")
        rule = store.find(MODULE, int(snapshot.get("规则ID", -1)))
        if rule is not None and int(rule.get("生效快照ID") or -1) == int(snapshot.get("id", -1)):
            rule["生效快照ID"] = None
            rule["生效版本"] = None
        self._sync_rule_pointers()
        return snapshot, f"快照 {snapshot.get('版本号')} 已停用"

    def _recalculate_snapshot_locked(self, snapshot: dict[str, Any], actor: str) -> tuple[dict[str, Any] | None, str]:
        if snapshot.get("status") == "已归档":
            return None, f"快照 {snapshot.get('版本号')} 已归档，只读历史不能重算"
        old_digest = snapshot.get("摘要")
        new_digest = compute_digest(snapshot, normalize_version(snapshot.get("版本号")), "生效口径")
        if old_digest:
            snapshot.setdefault("旧摘要", []).append({
                "摘要": old_digest, "口径": snapshot.get("摘要口径", "生效口径"), "记录时间": now_text(),
            })
        snapshot["摘要口径"] = "生效口径"
        snapshot["摘要"] = new_digest
        snapshot["选择器摘要"] = _selector_summary(snapshot)
        _append_audit(snapshot, "重算摘要", actor, "按生效口径重算快照摘要，旧摘要保留可查",
                      from_status=snapshot.get("status"), to_status=snapshot.get("status"),
                      digest_before=old_digest, digest_after=new_digest)
        return snapshot, f"快照 {snapshot.get('版本号')} 摘要已按生效口径重算"

    # -------------------------------------------------------------- 运行任务
    def list_runs(self, *, rule_id: int | None = None, snapshot_id: int | None = None,
                  status: str | None = None) -> list[dict[str, Any]]:
        self._ensure_ready()
        rows = [dict(row) for row in store.rows(RUN_MODULE)]
        if rule_id is not None:
            rows = [row for row in rows if int(row.get("规则ID", -1)) == int(rule_id)]
        if snapshot_id is not None:
            rows = [row for row in rows if int(row.get("快照ID", -1)) == int(snapshot_id)]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        rows.sort(key=lambda row: int(row.get("id", 0)))
        return rows

    def start_run(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """启动一次采集：只能引用一个已发布且测试通过的快照。"""
        self._ensure_ready()
        with _LOCK:
            snapshot_id = int(values.get("快照ID") or values.get("snapshot_id") or 0)
            snapshot = store.find(SNAPSHOT_MODULE, snapshot_id) if snapshot_id else None
            if snapshot is None:
                return None, "启动采集必须引用一个采集规则发布快照"
            if snapshot.get("status") != "已发布":
                return None, f"快照 {snapshot.get('版本号')} 当前状态为「{snapshot.get('status')}」，生产采集只能读取已发布快照"
            if not _test_fields_pass(self._test_for_snapshot(snapshot)):
                return None, "快照缺少同版本通过的选择器测试结果，不能用于生产采集"
            rule = store.find(MODULE, int(snapshot.get("规则ID", -1)))
            if rule is not None and rule.get("status") == "已停用":
                return None, "采集规则已停用，不能再启动生产采集"
            if rule is not None and int(rule.get("生效快照ID") or -1) != int(snapshot.get("id", -1)):
                return None, "该快照不是当前生效版本，生产采集不能引用多个规则版本"
            run = {
                "id": _next_id(RUN_MODULE),
                "status": "运行中",
                "pending": True,
                "abnormal": False,
                "运行编号": f"RUN-{snapshot.get('版本号')}-{_next_id(RUN_MODULE):04d}",
                "规则ID": snapshot.get("规则ID"),
                "规则编号": snapshot.get("规则编号"),
                "快照ID": snapshot.get("id"),
                "版本号": snapshot.get("版本号"),
                "关键指标": f"{snapshot.get('版本号')} / 运行中",
                "关联任务": values.get("关联任务") or "生产采集任务",
                "抓取条数": 0,
                "选择器错误": 0,
                "开始时间": now_text(),
                "结束时间": None,
                "操作人": values.get("操作人") or "调度器",
            }
            store.rows(RUN_MODULE).append(run)
            return run, f"采集任务已启动，引用快照 {snapshot.get('版本号')}"

    def complete_run(self, run_id: int, values: dict[str, Any] | None = None,
                     actor: str | None = None) -> tuple[dict[str, Any] | None, str]:
        self._ensure_ready()
        values = values or {}
        with _LOCK:
            run = store.find(RUN_MODULE, run_id)
            if run is None:
                return None, f"采集任务 {run_id} 不存在"
            if run.get("status") != "运行中":
                return None, f"采集任务 {run.get('运行编号')} 已结束，不能重复推进"
            run["抓取条数"] = int(values.get("抓取条数") or run.get("抓取条数") or 0)
            run["选择器错误"] = int(values.get("选择器错误") or run.get("选择器错误") or 0)
            run["status"] = "已完成"
            run["pending"] = False
            run["abnormal"] = int(run["选择器错误"]) > 0
            run["结束时间"] = now_text()
            run["操作人"] = actor or run.get("操作人") or "调度器"
            promotion_message = self._promote_pending_effective(int(run.get("规则ID", -1)), actor or "调度器")
            message = f"采集任务 {run.get('运行编号')} 已完成"
            if promotion_message:
                message = f"{message}；{promotion_message}"
            return run, message

    def _promote_pending_effective(self, rule_id: int, actor: str) -> str:
        """旧快照最后一个运行任务结束后，把待生效快照推进为已发布。"""
        rule = store.find(MODULE, rule_id)
        if rule is None or rule.get("status") == "已停用":
            return ""
        pending = [s for s in store.rows(SNAPSHOT_MODULE)
                   if int(s.get("规则ID", -1)) == int(rule_id) and s.get("status") == "待生效"]
        if not pending:
            return ""
        pending.sort(key=lambda row: int(row.get("id", 0)))
        candidate = pending[-1]
        baseline_id = int(candidate.get("基线快照ID") or -1)
        if baseline_id > 0 and self._has_running_runs(baseline_id):
            return f"旧快照仍有运行中任务，候选快照 {candidate.get('版本号')} 保持待生效"
        old_digest = candidate.get("摘要")
        snapshot, message = self._publish_snapshot_locked(
            candidate, actor, old_digest,
            f"基线快照 {candidate.get('基线版本')} 的运行任务已全部结束，测试结果自动推进",
            audit_action="自动生效")
        return message

    def effective_snapshot(self, rule_id: int) -> tuple[dict[str, Any] | None, str]:
        self._ensure_ready()
        rule = store.find(MODULE, rule_id)
        if rule is None:
            return None, f"采集规则 {rule_id} 不存在或已归档"
        snapshot = self._current_published(rule_id)
        if snapshot is None:
            return None, f"采集规则 {rule_id} 暂无已发布快照"
        return dict(snapshot), f"采集规则 {rule_id} 当前生效版本 {snapshot.get('版本号')}"

    # ------------------------------------------------------- 与监控分析服务的边界
    def _write_rule_review(self, rule: dict[str, Any], snapshot: dict[str, Any], todo_note: str) -> None:
        """把快照核对基线写回采集规则，供监控服务消费时对比差异。"""
        review = dict(rule.get("监控核对") or {})
        history = list(review.get("历史") or [])
        entry = {
            "快照ID": snapshot.get("id"),
            "版本号": snapshot.get("版本号"),
            "状态": snapshot.get("status"),
            "摘要": snapshot.get("摘要"),
            "记录时间": now_text(),
            "说明": todo_note,
        }
        history.append(entry)
        review.update(entry)
        review["历史"] = history[-20:]
        rule["监控核对"] = review
        rule["核对结果"] = f"{snapshot.get('版本号')} {snapshot.get('status')}"

    def record_monitor_review(self, rule_id: int, snapshot_id: int, result: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """监控回写：把指定快照的核对结果落到采集规则上。"""
        self._ensure_ready()
        with _LOCK:
            rule = store.find(MODULE, rule_id)
            snapshot = store.find(SNAPSHOT_MODULE, snapshot_id)
            if rule is None:
                return None, f"采集规则 {rule_id} 不存在或已归档"
            if snapshot is None:
                return None, f"采集规则快照 {snapshot_id} 不存在"
            if int(snapshot.get("规则ID", -1)) != int(rule_id):
                return None, "快照与采集规则不匹配，不能回写核对结果"
            review = dict(rule.get("监控核对") or {})
            history = list(review.get("历史") or [])
            history.append({
                "快照ID": snapshot_id,
                "版本号": snapshot.get("版本号"),
                "抓取条数": result.get("抓取条数", 0),
                "选择器错误": result.get("选择器错误", 0),
                "差异": result.get("差异", ""),
                "记录时间": now_text(),
                "来源": "采集监控",
            })
            review["最近核对"] = history[-1]
            review["历史"] = history[-20:]
            rule["监控核对"] = review
            rule["核对结果"] = (
                f"{snapshot.get('版本号')} 抓取{result.get('抓取条数', 0)}条 "
                f"选择器错误{result.get('选择器错误', 0)}"
            )
            rule["核对时间"] = now_text()
            return rule, f"采集规则 {rule_id} 已回写快照 {snapshot.get('版本号')} 的核对结果"
