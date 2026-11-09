#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""决策表边界测试。改动 decide.py 的阈值后必须先跑通这里再上线。

    uv run scripts/test_decide.py
"""

import json
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from decide import (
    DEFAULT_BASE, DEFAULT_COOLDOWN_MINUTES, MONITOR_THRESHOLD,
    decide_delta, decide_write_mode, evaluate,
)


class TestBands(unittest.TestCase):
    """浅度超成本率档位。正值压价、负值顶价、|x|<0.05 不动。"""

    def assert_delta(self, rate: str, expected: str, action: str) -> None:
        band, got_action, delta = decide_delta(Decimal(rate))
        self.assertEqual(
            str(delta), expected, f"rate={rate} 命中区间 {band}，增量应为 {expected}"
        )
        self.assertEqual(got_action, action, f"rate={rate} 动作应为 {action}")

    # 压价侧：超成本了
    def test_deep_cut_at_and_above_100(self):
        self.assert_delta("1.00", "-0.30", "压价")
        self.assert_delta("1.85", "-0.30", "压价")

    def test_cut_020_band(self):
        self.assert_delta("0.50", "-0.20", "压价")
        self.assert_delta("0.9999", "-0.20", "压价")

    def test_cut_015_band(self):
        self.assert_delta("0.40", "-0.15", "压价")
        self.assert_delta("0.4999", "-0.15", "压价")

    def test_cut_010_band(self):
        self.assert_delta("0.30", "-0.10", "压价")
        self.assert_delta("0.3999", "-0.10", "压价")

    def test_cut_005_band(self):
        self.assert_delta("0.20", "-0.05", "压价")
        self.assert_delta("0.2999", "-0.05", "压价")

    def test_cut_003_band(self):
        self.assert_delta("0.11", "-0.03", "压价")
        self.assert_delta("0.1999", "-0.03", "压价")

    def test_cut_002_band(self):
        self.assert_delta("0.05", "-0.02", "压价")
        self.assert_delta("0.1099", "-0.02", "压价")

    # 观察区
    def test_observation_band_is_symmetric(self):
        for r in ("0.0499", "0.00", "-0.0499"):
            self.assert_delta(r, "0.00", "不操作")

    # 顶价侧：没超成本、有余量
    def test_top_002_band(self):
        self.assert_delta("-0.05", "0.02", "顶价")
        self.assert_delta("-0.1099", "0.02", "顶价")

    def test_top_003_band(self):
        self.assert_delta("-0.11", "0.03", "顶价")
        self.assert_delta("-0.1999", "0.03", "顶价")

    def test_top_005_band(self):
        self.assert_delta("-0.20", "0.05", "顶价")
        self.assert_delta("-0.2999", "0.05", "顶价")

    def test_top_010_band(self):
        self.assert_delta("-0.30", "0.10", "顶价")
        self.assert_delta("-0.85", "0.10", "顶价")

    def test_custom_positive_040_threshold(self):
        """压价细分档：>=1.00 压 -0.30，0.50~1.00 压 -0.20，0.40~0.50 压 -0.15，0.30~0.40 压 -0.10。"""
        self.assert_delta("0.35", "-0.10", "压价")
        self.assert_delta("0.40", "-0.15", "压价")
        self.assert_delta("-0.35", "0.10", "顶价")

    def test_no_gap_across_grid(self):
        rate = Decimal("-1.00")
        step = Decimal("0.0001")
        while rate <= Decimal("1.00"):
            decide_delta(rate)  # 不抛异常即说明无空档
            rate += step


class TestEvaluate(unittest.TestCase):
    """端到端：超成本率定档 + 基数 + 系数封顶/封底。"""

    def test_missing_base_uses_default_and_marks_source(self):
        # base=1.0（无基数），rate=+0.08 -> 压价 -0.02 -> 新系数 0.98（不触下限）
        d = evaluate("1001", "0.08")
        self.assertEqual(d.base_used, "1.00")
        self.assertEqual(d.base_source, "新建基数")
        self.assertEqual(d.new_coefficient, "0.98")
        self.assertEqual(d.status, "OK")

    def test_existing_base_is_respected(self):
        d = evaluate("1002", "-0.15", "0.72")
        self.assertEqual(d.base_source, "平台现有基数")
        self.assertEqual(d.new_coefficient, "0.75")

    def test_observation_band_never_produces_a_coefficient(self):
        # 观察区不写入，所以不能算出新系数（哪怕平台已有基数）
        for base in (None, "0.60", "0.72"):
            d = evaluate("1010", "0.02", base)
            self.assertEqual(d.status, "NO_ACTION")
            self.assertEqual(d.action, "不操作")
            self.assertEqual(d.delta, "0.00")
            self.assertEqual(d.new_coefficient, "-")
            self.assertEqual(d.base_used, "-")

    def test_deep_cut_from_default_base(self):
        d = evaluate("1003", "0.42")
        self.assertEqual(d.delta, "-0.15")
        self.assertEqual(d.new_coefficient, "0.85")



    def test_missing_rate_is_skipped(self):
        """超成本率缺失时必须跳过，不能当 0 当不动处理。

        当 0 会让数据残缺的账户静默进观察区，看报告的人以为它正常。
        """
        for raw in ("", "-", "null", None, "abc", "5%"):
            d = evaluate("1012", raw)
            self.assertEqual(d.status, "SKIP_NO_DATA", f"rate={raw!r} 应跳过")
            self.assertEqual(d.new_coefficient, "-")

    def test_cap_applies_instead_of_hold(self):
        # 顶价后超上限直接封顶，不再转人工
        d = evaluate("1005", "-0.42", "1.35")
        self.assertEqual(d.status, "OK")
        self.assertEqual(d.new_coefficient, "1.40")
        # 压价后破下限直接封底（0.42 -> -0.15，0.20-0.15=0.05 < 0.10）
        d = evaluate("1006", "0.42", "0.20")
        self.assertEqual(d.status, "OK")
        self.assertEqual(d.new_coefficient, "0.10")

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertEqual(evaluate("1007", " -0.08 ").delta, "+0.02")
        self.assertEqual(evaluate("1008", "\t0.15\n").delta, "-0.03")

    def test_percent_value_is_refused_instead_of_guessed(self):
        # "15%" 若被当成 15 会误判成最高档压价，必须拒绝而不是猜口径
        for raw in ("15%", "0.15%", "-"):
            self.assertEqual(evaluate("1009", raw).status, "SKIP_NO_DATA")

    def test_aggregate_row_is_rejected(self):
        # 导出表的「合计」行数值看着完全正常，绝不能判成账户
        for acc in ("合计", "总计", "汇总", "小计", "Total", "全部账户"):
            d = evaluate(acc, "0.15")
            self.assertEqual(d.status, "SKIP_NOT_ACCOUNT", f"{acc} 应被剔除")
            self.assertEqual(d.new_coefficient, "-")

    def test_non_numeric_account_is_rejected(self):
        d = evaluate("账户ID", "0.15")
        self.assertEqual(d.status, "SKIP_NOT_ACCOUNT")

    def test_normal_numeric_account_passes(self):
        d = evaluate("8801234", "0.15")
        self.assertEqual(d.status, "OK")

    def test_default_base_constant(self):
        self.assertEqual(DEFAULT_BASE, Decimal("1.0"))


class TestRateSanity(unittest.TestCase):
    """超成本率绝对值过大时不转人工，仍正常按最高/最低档定档写入，并标记“持续监控”。

    实测真实数据里有 +10.90（超成本 1090%）这种值，>=1.00 按最高档压 -0.30。
    """

    def test_extreme_positive_still_cuts_normally(self):
        for rate in ("10.90", "9.91", "4.69", "1.0001"):
            d = evaluate("114368154", rate, "0.60")
            self.assertEqual(d.status, "OK", f"rate={rate}")
            self.assertEqual(d.action, "压价", f"rate={rate}")
            self.assertEqual(d.delta, "-0.30", f"rate={rate}")
            self.assertIn("持续监控", d.note, f"rate={rate}")

    def test_extreme_negative_still_tops_normally(self):
        for rate in ("-10.90", "-1.0001"):
            d = evaluate("114368154", rate, "0.60")
            self.assertEqual(d.status, "OK", f"rate={rate}")
            self.assertEqual(d.action, "顶价", f"rate={rate}")
            self.assertEqual(d.delta, "+0.10", f"rate={rate}")
            self.assertIn("持续监控", d.note, f"rate={rate}")

    def test_boundary_is_not_monitored(self):
        # 恰好 ±1.00 不超过监控阈值，正常定档且不带持续监控标记
        d = evaluate("1", "1.00", "0.60")
        self.assertEqual(d.status, "OK")
        self.assertNotIn("持续监控", d.note)
        d = evaluate("1", "-1.00", "0.60")
        self.assertEqual(d.status, "OK")
        self.assertNotIn("持续监控", d.note)

    def test_monitor_threshold_constant(self):
        self.assertEqual(MONITOR_THRESHOLD, Decimal("1.0"))

    def test_extreme_keeps_rate_visible(self):
        # 异常值必须留在 rate 字段里，报告里要能看到原始超成本率
        d = evaluate("114368154", "10.90", "0.60")
        self.assertEqual(d.rate, "10.90")
        self.assertIn("持续监控", d.note)


class TestCooldown(unittest.TestCase):
    """触发间隔 20 分钟、窗口 30 分钟，相邻 2 轮读的是同一批数据。

    冷却期必须挡住这 6 轮里的后 5 轮，否则同一账户会被连续调整 6 次。
    """

    NOW = datetime(2026, 9, 14, 16, 29)

    def test_cooldown_is_disabled_by_default(self):
        # 窗口 20 分钟、触发 20 分钟，两轮零重叠，默认不需要冷却期
        self.assertEqual(DEFAULT_COOLDOWN_MINUTES, 0)

    def test_recent_adjustment_not_skipped_by_default(self):
        # 默认关闭冷却期：1 分钟前刚调过也照常判断
        d = evaluate("8801251", "0.42", "0.80", last_adjusted_at=self.NOW - timedelta(minutes=1), now=self.NOW)
        self.assertEqual(d.status, "OK")
        self.assertEqual(d.new_coefficient, "0.65")

    def test_cooldown_blocks_when_explicitly_enabled(self):
        # 参数仍保留：窗口调长后开启冷却期必须真的能拦住
        d = evaluate("8801251", "0.42", "0.80", last_adjusted_at=self.NOW - timedelta(minutes=5), now=self.NOW,
                     cooldown_minutes=30)
        self.assertEqual(d.status, "SKIP_COOLDOWN")
        self.assertEqual(d.new_coefficient, "-")
        self.assertEqual(d.delta, "0.00")
        # 区间信息保留，方便看清本来会怎么动
        self.assertEqual(d.band, "0.40 <= rate < 0.50")

    def test_enabled_cooldown_expires_at_boundary(self):
        d = evaluate("8801251", "0.42", "0.80", last_adjusted_at=self.NOW - timedelta(minutes=30), now=self.NOW,
                     cooldown_minutes=30)
        self.assertEqual(d.status, "OK")
        self.assertEqual(d.new_coefficient, "0.65")

    def test_no_ledger_entry_means_no_cooldown(self):
        d = evaluate("8801251", "0.42", "0.80", last_adjusted_at=None, now=self.NOW)
        self.assertEqual(d.status, "OK")

    def test_observation_band_checked_before_cooldown(self):
        # 观察区本来就不动，不该被报成冷却中
        d = evaluate("8801242", "0.02", "0.66", last_adjusted_at=self.NOW - timedelta(minutes=5), now=self.NOW,
                     cooldown_minutes=30)
        self.assertEqual(d.status, "NO_ACTION")

    def test_aggregate_row_checked_before_cooldown(self):
        d = evaluate("合计", "0.15", None, last_adjusted_at=self.NOW - timedelta(minutes=5), now=self.NOW,
                     cooldown_minutes=30)
        self.assertEqual(d.status, "SKIP_NOT_ACCOUNT")


class WriteModeTests(unittest.TestCase):
    """新建 vs 原地改的判定。

    判错的后果不对称：该新建却原地改 = 改掉别人的出价（实测有策略覆盖 186
    个账户）；该原地改却新建 = 多一条策略，无害。所以所有不确定的情况都
    必须倒向 CREATE。
    """

    OWNED = "短剧-自动-114368154"

    def _entry(self, ids, name):
        return {
            "strategy_id": 26007, "name": name,
            "coefficient": 0.65, "account_ids": ids,
            "account_count": len(ids),
        }

    def test_solo_and_owned_can_update(self):
        d = decide_write_mode(self._entry(["114368154"], self.OWNED), "114368154")
        self.assertEqual(d["write_mode"], "UPDATE")
        self.assertEqual(d["target_strategy_id"], 26007)

    def test_other_accounts_force_create(self):
        d = decide_write_mode(
            self._entry(["114368154", "120163530"], self.OWNED), "114368154"
        )
        self.assertEqual(d["write_mode"], "CREATE")
        self.assertEqual(d["other_account_count"], 1)

    def test_foreign_strategy_never_updated(self):
        # 别人手工建的策略，即使只含本账户也不碰
        d = decide_write_mode(
            self._entry(["121584221"], "IAA_ROI_121584221_1.05_0811"), "121584221"
        )
        self.assertEqual(d["write_mode"], "CREATE")

    def test_big_cluster_forces_create(self):
        # 真实场景：一条策略覆盖 186 个账户
        ids = [str(120163530 + i) for i in range(186)]
        d = decide_write_mode(self._entry(ids, self.OWNED), "120163530")
        self.assertEqual(d["write_mode"], "CREATE")
        self.assertEqual(d["other_account_count"], 185)

    def test_prefix_match_is_exact(self):
        # 名字里含前缀但不在开头，不算自己的
        d = decide_write_mode(
            self._entry(["114368154"], "旧_短剧-自动-114368154"), "114368154"
        )
        self.assertEqual(d["write_mode"], "CREATE")

    def test_empty_ids_forces_create(self):
        d = decide_write_mode(self._entry([], self.OWNED), "114368154")
        self.assertEqual(d["write_mode"], "UPDATE")


def _emit(ok: bool, code: int, error: str | None, msg: str) -> None:
    """按 Response Contract 在 stdout 最后一行输出机器可读结果。

    退出码与这里的 code 必须一致：调用方先读 code，再按
    references/error-policy.json 的有限动作映射决定下一步。
    """
    payload: dict[str, object] = {
        "ok": ok,
        "code": code, "duration_ms": 0,
        "msg": msg,
        "next_action": {
            "type": "CONTINUE" if ok else "STOP",
            "retryable": False,
        },
    }
    if error:
        payload["error"] = error
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    if result.wasSuccessful():
        _emit(True, 0, None, f"{result.testsRun} 项测试全部通过")
        sys.exit(0)
    _emit(False, 1, "TEST_FAILED",
          f"失败 {len(result.failures)} 项，错误 {len(result.errors)} 项")
    sys.exit(1)
