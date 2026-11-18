"""采集规则发布快照与监控消费的回归测试（纯服务层，不依赖 HTTP）。"""
from __future__ import annotations

import threading
import unittest

from app import seed
from app.services import crawler as crawler_module
from app.services.crawler import CrawlerService
from app.services.monitor import MonitorService
from app.store import store


def reset_store() -> None:
    store._tables = {name: [dict(row) for row in rows] for name, rows in seed.SEED_ROWS.items()}
    crawler_module._READY = False


class SnapshotLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.crawler = CrawlerService()
        self.monitor = MonitorService()

    def test_test_pass_creates_candidate_without_touching_published(self) -> None:
        before = self.crawler.effective_snapshot(3)[0]
        snapshot, message = self.crawler.submit_test(3, {"正文选择器": "div.new-body"})
        self.assertIsNotNone(snapshot)
        self.assertIn("待发布", message)
        after = self.crawler.effective_snapshot(3)[0]
        self.assertEqual(before["版本号"], after["版本号"])
        self.assertEqual(before["正文选择器"], after["正文选择器"])
        test = self.crawler.get_test(int(snapshot["测试ID"]))
        self.assertEqual(test["版本号"], snapshot["版本号"])
        self.assertEqual(test["正文选择器"], snapshot["正文选择器"])

    def test_publish_requires_explicit_confirmation(self) -> None:
        self.crawler.submit_test(3, {"正文选择器": "div.new-body"})
        # 提交测试本身不发布；必须再显式确认候选快照。
        self.assertEqual(self.crawler.effective_snapshot(3)[0]["正文选择器"], "网页采集规则样例3")
        candidate = self.crawler.list_snapshots(rule_id=3, status="待发布")[0]
        published, message = self.crawler.run_snapshot_action(int(candidate["id"]), "确认发布")
        self.assertIsNotNone(published, message)
        self.assertEqual(self.crawler.effective_snapshot(3)[0]["正文选择器"], "div.new-body")

    def test_same_snapshot_only_one_confirmation_succeeds(self) -> None:
        self.crawler.submit_test(3, {"正文选择器": "div.new-body"})
        candidate = self.crawler.list_snapshots(rule_id=3, status="待发布")[0]
        results: list[tuple[bool, str]] = []

        def confirm() -> None:
            snapshot, message = self.crawler.run_snapshot_action(int(candidate["id"]), "确认发布")
            results.append((snapshot is not None, message))

        threads = [threading.Thread(target=confirm) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(1 for ok, _ in results if ok), 1, results)
        rejected = [message for ok, message in results if not ok]
        self.assertTrue(rejected and "不能再次确认发布" in rejected[0], results)

    def test_pending_effective_promotes_after_old_run_finishes(self) -> None:
        run, _ = self.crawler.start_run({"快照ID": 2})
        self.crawler.submit_test(3, {"正文选择器": "div.new-body"})
        candidate = self.crawler.list_snapshots(rule_id=3, status="待发布")[0]
        pending, message = self.crawler.run_snapshot_action(int(candidate["id"]), "确认发布")
        self.assertIsNotNone(pending, message)
        self.assertEqual(pending["status"], "待生效")
        # 旧快照仍是生产读取的唯一版本。
        self.assertEqual(self.crawler.effective_snapshot(3)[0]["版本号"], "v1")
        self.crawler.complete_run(int(run["id"]), {"抓取条数": 30})
        self.assertEqual(self.crawler.effective_snapshot(3)[0]["版本号"], candidate["版本号"])

    def test_failed_test_does_not_create_candidate(self) -> None:
        test, _ = self.crawler.submit_test(1, {"页面选择器": "div.list"}, auto_run=False)
        failed, message = self.crawler.complete_test(int(test["id"]), {"结果": "失败", "选择器错误": 4})
        self.assertIsNotNone(failed, message)
        self.assertEqual(failed["结果"], "失败")
        self.assertEqual(self.crawler.list_snapshots(rule_id=1), [])

    def test_archive_is_terminal(self) -> None:
        self.crawler.submit_test(3, {"正文选择器": "div.new-body"})
        candidate = self.crawler.list_snapshots(rule_id=3, status="待发布")[0]
        self.crawler.run_snapshot_action(int(candidate["id"]), "归档")
        snapshot, message = self.crawler.run_snapshot_action(int(candidate["id"]), "确认发布")
        self.assertIsNone(snapshot)
        self.assertIn("已归档", message)

    def test_run_requires_published_snapshot(self) -> None:
        pending = self.crawler.get_snapshot(1)
        self.assertEqual(pending["status"], "待发布")
        run, message = self.crawler.start_run({"快照ID": 1})
        self.assertIsNone(run)
        self.assertIn("只能读取已发布快照", message)

    def test_version_mismatch_between_test_and_snapshot_is_rejected(self) -> None:
        self.crawler.submit_test(3, {"正文选择器": "div.new-body"})
        candidate = self.crawler.get_snapshot(int(self.crawler.list_snapshots(rule_id=3, status="待发布")[0]["id"]))
        candidate["版本号"] = "v9"
        snapshot, message = self.crawler.run_snapshot_action(int(candidate["id"]), "确认发布")
        self.assertIsNone(snapshot)
        self.assertIn("不一致", message)

    def test_second_test_while_candidate_pending_is_archived_not_overlaid(self) -> None:
        first, _ = self.crawler.submit_test(3, {"正文选择器": "div.v2"})
        second, message = self.crawler.submit_test(3, {"正文选择器": "div.v3"})
        self.assertIsNotNone(second, message)
        self.assertEqual(second["结果"], "通过")
        self.assertIsNone(second["快照ID"])
        self.assertTrue(second["重复候选"])
        candidates = self.crawler.list_snapshots(rule_id=3, status="待发布")
        self.assertEqual([item["id"] for item in candidates], [first["id"]])

    def test_monitor_groups_by_version_and_writes_back(self) -> None:
        metrics, message = self.monitor.sync_from_crawler()
        self.assertTrue(metrics)
        versions = {row["版本号"] for row in metrics}
        self.assertIn("v1", versions)
        rule = self.crawler.get_entry(3)
        self.assertIsNotNone(rule.get("监控核对"))
        self.assertIn("v1", rule.get("核对结果", ""))
        todo = self.monitor.list_todos()[0]
        started, _ = self.monitor.run_todo_action(int(todo["id"]), "开始处理")
        self.assertEqual(started["status"], "处理中")
        done, _ = self.monitor.run_todo_action(int(todo["id"]), "标记已处理")
        self.assertEqual(done["status"], "已处理")
        again, message = self.monitor.run_todo_action(int(todo["id"]), "标记已处理")
        self.assertIsNone(again)
        self.assertIn("已结束", message)


if __name__ == "__main__":
    unittest.main()
