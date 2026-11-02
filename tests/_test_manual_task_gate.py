"""手动任务入口的风控门禁（manual_task_gate + 七个面板路由接线）。

调度器巡检动手前查两道闸（账号级 restrictedUntil + 区域级连坐暂停，见
wb_scheduler 的 inspect 循环），API 派发在 open_upstream 里快速失败；
唯独手动按钮一道都没有——面板上点一下就直接发上游请求，账号被限制时
继续喂 403 / 11140，连坐暂停期间还把整池暂停当空气。本套件钉住：

1. helper 语义：区域暂停整单拒绝 / 单账号受限整单拒绝 / 混合名单剔除
   受限号 / 全部受限整单拒绝 / 干净名单原样放行；
2. 路由接线：run / streak / travel / checkin / daily-chat / daily-chat-web
   在区域暂停时一个上游调用都不发，受限号在结果里可见地跳过。

Run with: python tests/_test_manual_task_gate.py
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy


class FakeAccount(object):
    def __init__(self, uid, realm="cn", nickname=None, restricted_until=0.0):
        self.uid = uid
        self.realm = realm
        self.nickname = nickname
        self.restricted_until = restricted_until
        self.calls = 0
        self.enabled = True
        self.tasks_enabled = True

    def task_active(self):
        return True

    def uses_enterprise_billing(self):
        return False

    def checkin(self, trigger="unknown"):
        self.calls += 1
        return {"ok": True, "msg": "签到成功"}

    def daily_chat(self, trigger="unknown"):
        self.calls += 1
        return {"ok": True, "msg": "已打卡"}

    def daily_chat_web(self, prompt=None, trigger="unknown"):
        self.calls += 1
        return {"ok": True, "conversation": "ok"}


class FakePool(object):
    def __init__(self, accounts):
        self.accounts = accounts

    def get(self, uid):
        for account in self.accounts:
            if account.uid == uid:
                return account
        return None

    def task_accounts(self, realm=None):
        return [a for a in self.accounts if not realm or a.realm == realm]


def _restricted_account(uid, nickname=None, seconds=1800.0, realm="cn"):
    return FakeAccount(uid, realm=realm, nickname=nickname,
                       restricted_until=time.time() + seconds)


def _hold_realm(realm, seconds=1800.0):
    """让 restriction_hold_remaining(realm) 返回正数。"""
    patcher = mock.patch.object(
        wb_proxy, "restriction_hold_remaining",
        lambda r, now=None: seconds if r == realm else 0.0)
    patcher.start()
    return patcher


class GateHelperTests(unittest.TestCase):
    def test_clean_targets_pass_through_untouched(self):
        accounts = [FakeAccount("a"), FakeAccount("b")]
        refusal, allowed, skipped = wb_proxy.manual_task_gate("cn", accounts)
        self.assertIsNone(refusal)
        self.assertEqual(allowed, accounts)
        self.assertEqual(skipped, [])

    def test_realm_hold_refuses_whole_batch(self):
        patcher = _hold_realm("cn")
        self.addCleanup(patcher.stop)
        refusal, allowed, skipped = wb_proxy.manual_task_gate(
            "cn", [FakeAccount("a")])
        self.assertIn("暂停派发", refusal)
        self.assertEqual(allowed, [])
        self.assertEqual(skipped, [])

    def test_restricted_accounts_are_filtered_with_visible_lines(self):
        accounts = [FakeAccount("ok1"),
                    _restricted_account("bad1", nickname="受限号")]
        refusal, allowed, skipped = wb_proxy.manual_task_gate("cn", accounts)
        self.assertIsNone(refusal)
        self.assertEqual([a.uid for a in allowed], ["ok1"])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0][0].uid, "bad1")
        self.assertIn("风控限制窗口", skipped[0][1])
        self.assertIn("已跳过", skipped[0][1])

    def test_single_restricted_target_refuses_whole_call(self):
        target = _restricted_account("only")
        refusal, allowed, skipped = wb_proxy.manual_task_gate("cn", [target])
        self.assertIn("风控限制窗口", refusal)
        self.assertEqual(allowed, [])
        self.assertEqual(len(skipped), 1)

    def test_all_restricted_refuses_with_count(self):
        accounts = [_restricted_account("a"), _restricted_account("b")]
        refusal, allowed, _ = wb_proxy.manual_task_gate("cn", accounts)
        self.assertIn("全部 2 个账号", refusal)
        self.assertEqual(allowed, [])

    def test_none_targets_are_dropped_silently(self):
        refusal, allowed, skipped = wb_proxy.manual_task_gate("cn", [None])
        self.assertIsNone(refusal)
        self.assertEqual(allowed, [])
        self.assertEqual(skipped, [], "None 不是受限，不能算进拒绝计数")

    def test_missing_restricted_until_attr_counts_as_clean(self):
        bare = FakeAccount("bare")
        del bare.restricted_until
        refusal, allowed, skipped = wb_proxy.manual_task_gate("cn", [bare])
        self.assertIsNone(refusal)
        self.assertEqual(allowed, [bare])


class RouteTests(unittest.TestCase):
    """路由接线：区域暂停/单号受限时一个上游调用都不发。"""

    class Handler(wb_proxy.Handler):
        def __init__(self):
            self.captured = None

        def _json(self, code, obj):
            self.captured = (code, obj)

        def _error(self, code, message, err_type="server_error"):
            self.captured = (code, {"error": message})

    def setUp(self):
        self.accounts = [
            FakeAccount("c1", nickname="正常号"),
            _restricted_account("c2", nickname="受限号"),
            FakeAccount("i1", realm="intl", nickname="国际号"),
            _restricted_account("i2", realm="intl", nickname="受限国际号"),
        ]
        self.pool = FakePool(self.accounts)
        for target, value in (("POOL", self.pool),
                              ("log", lambda *a, **k: None),
                              ("add_log_entry", lambda *a, **k: None),
                              ("invalidate_tasks_cache", lambda: None),
                              ("account_views", lambda realm=None: [])):
            patcher = mock.patch.object(wb_proxy, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, method, payload):
        handler = self.Handler()
        getattr(handler, method)(payload)
        code, body = handler.captured
        self.assertEqual(code, 200)
        return body

    def called(self):
        return sorted(a.uid for a in self.accounts if a.calls)

    def test_tasks_run_skips_restricted_and_reports_it(self):
        wb_tasks = mock.MagicMock()
        wb_tasks.run_growth_tasks.return_value = {"ok": True, "logs": [],
                                                  "earned_credit": 0}
        with mock.patch.dict(sys.modules, {"wb_tasks": wb_tasks}):
            body = self.call("_route_tasks_run", {})
        self.assertTrue(body["ok"])
        self.assertEqual(body["accounts_count"], 1)
        self.assertEqual(
            [uid for uid in (wb_tasks.run_growth_tasks.call_args_list[0][0][0].uid,)],
            ["c1"], "只有未受限的号跑任务")
        self.assertTrue(any("受限号" in line and "已跳过" in line
                            for line in body["logs"]),
                        "跳过必须写进日志输出: %r" % body["logs"])

    def test_tasks_run_refuses_during_realm_hold(self):
        patcher = _hold_realm("cn")
        self.addCleanup(patcher.stop)
        body = self.call("_route_tasks_run", {})
        self.assertFalse(body["ok"])
        self.assertIn("暂停派发", body["msg"])
        self.assertEqual(self.called(), [])

    def test_tasks_claim_refuses_a_restricted_single_account(self):
        body = self.call("_route_tasks_claim", {"uid": "c2", "code": "x"})
        self.assertFalse(body["ok"])
        self.assertIn("风控限制窗口", body["msg"])
        self.assertEqual(self.called(), [])

    def test_streak_bonus_and_travel_skip_restricted_accounts(self):
        wb_tasks = mock.MagicMock()
        wb_tasks.run_streak_bonus.return_value = {"ok": True, "msg": "完成"}
        wb_tasks.do_cat_travel.return_value = {"ok": True, "action": "claim",
                                               "msg": "领奖", "credit": 5}
        with mock.patch.dict(sys.modules, {"wb_tasks": wb_tasks}):
            streak = self.call("_route_tasks_streak_bonus", {})
            travel = self.call("_route_tasks_travel", {})
        self.assertTrue(streak["ok"])
        self.assertTrue(any("受限号" in line for line in streak["msg"].splitlines()))
        self.assertTrue(travel["ok"])
        skipped_rows = [r for r in travel["results"] if r.get("action") == "skip"]
        self.assertEqual([r["uid"] for r in skipped_rows], ["c2"])
        self.assertEqual(self.called(), [])

    def test_checkin_skips_restricted_with_result_row(self):
        body = self.call("_route_accounts_checkin", {})
        rows = {r["uid"]: r for r in body["results"]}
        self.assertTrue(rows["c1"]["ok"])
        self.assertFalse(rows["c2"]["ok"])
        self.assertTrue(rows["c2"]["restricted"])
        self.assertIn("风控限制窗口", rows["c2"]["error"])
        self.assertEqual(self.called(), ["c1"])

    def test_daily_chat_gates_intl_realm(self):
        patcher = _hold_realm("intl")
        self.addCleanup(patcher.stop)
        body = self.call("_route_accounts_daily_chat", {})
        self.assertFalse(body["ok"])
        self.assertIn("暂停派发", body["msg"])
        self.assertEqual(self.called(), [])

    def test_daily_chat_web_skips_restricted_intl_accounts(self):
        body = self.call("_route_accounts_daily_chat_web", {})
        rows = {r["uid"]: r for r in body["results"]}
        self.assertTrue(rows["i1"]["ok"])
        self.assertTrue(rows["i2"]["restricted"])
        self.assertEqual(self.called(), ["i1"])

    def test_daily_chat_web_refuses_during_intl_hold(self):
        patcher = _hold_realm("intl")
        self.addCleanup(patcher.stop)
        body = self.call("_route_accounts_daily_chat_web", {})
        self.assertFalse(body["ok"])
        self.assertIn("暂停派发", body["msg"])
        self.assertEqual(self.called(), [])


if __name__ == "__main__":
    unittest.main()
