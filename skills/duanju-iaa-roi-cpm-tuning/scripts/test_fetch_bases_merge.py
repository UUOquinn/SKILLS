#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""fetch_bases.py --merge-csv 回填逻辑测试。改动回填代码后必须先跑通这里再上线。

    uv run scripts/test_fetch_bases_merge.py
"""

import csv
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_bases import fill_bases_into_csv  # noqa: E402


class TestFillBases(unittest.TestCase):
    def _run(self, src_text: str, bases: dict) -> tuple[list[dict], Path]:
        tmp = Path(tempfile.mkdtemp())
        src = tmp / "roi.csv"
        src.write_text(src_text, encoding="utf-8")
        out = tmp / "roi_with_base.csv"
        n = fill_bases_into_csv(src, bases, out)
        with out.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        return rows, out

    def test_basic_fill_and_no_base_clear(self):
        rows, out = self._run(
            "account_id,rate,current_coefficient,cost,minutes\n"
            "113174052,0.2156512988,0.65,123,20\n"
            "117794123,-0.0217529644,0.42,456,20\n"
            "119632691,-0.0886729196,0.46,789,20\n",
            {
                "113174052": {"base": "0.60", "base_source": "PLATFORM_UNIQUE"},
                "117794123": {"base": None, "base_source": "NO_BASE"},
                # 119632691 不在 bases 里：保持原值
            },
        )
        self.assertEqual(rows[0]["current_coefficient"], "0.60")
        self.assertEqual(rows[1]["current_coefficient"], "")
        self.assertEqual(rows[2]["current_coefficient"], "0.46")
        # 其他列与表头顺序保持
        self.assertEqual(rows[0]["account_id"], "113174052")
        self.assertEqual(rows[0]["cost"], "123")
        self.assertEqual(
            list(rows[0].keys()),
            ["account_id", "rate", "current_coefficient", "cost", "minutes"],
        )

    def test_missing_column_gets_added(self):
        rows, out = self._run(
            "account_id,rate,cost\n114368154,0.12,500\n",
            {"114368154": {"base": 0.70, "base_source": "PLATFORM_UNIQUE"}},
        )
        self.assertIn("current_coefficient", rows[0])
        from decimal import Decimal
        self.assertEqual(Decimal(rows[0]["current_coefficient"]), Decimal("0.70"))

    def test_chinese_account_column(self):
        rows, out = self._run(
            "账户id,rate,current_coefficient\n114368154,0.12,\n",
            {"114368154": {"base": 0.80}},
        )
        from decimal import Decimal
        self.assertEqual(Decimal(rows[0]["current_coefficient"]), Decimal("0.80"))

    def test_returns_updated_count(self):
        tmp = Path(tempfile.mkdtemp())
        src = tmp / "roi.csv"
        src.write_text("account_id,rate\n114368154,0.1\n119632691,0.2\n", encoding="utf-8")
        out = tmp / "roi_with_base.csv"
        n = fill_bases_into_csv(src, {"114368154": {"base": 0.9}}, out)
        self.assertEqual(n, 1)

    def test_utf8_sig_roundtrip(self):
        rows, out = self._run(
            "account_id,rate,current_coefficient\n114368154,0.12,0.66\n",
            {"114368154": {"base": "0.66"}},
        )
        raw = out.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf") or raw.startswith(b"account_id"))
        self.assertEqual(rows[0]["current_coefficient"], "0.66")


if __name__ == "__main__":
    start = time.monotonic()
    try:
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromModule(sys.modules[__name__])
        runner = unittest.TextTestRunner(verbosity=2, stream=sys.stderr)
        result = runner.run(suite)
        ok = result.wasSuccessful()
        print(json.dumps({
            "ok": ok,
            "code": 0 if ok else 1,
            "duration_ms": int((time.monotonic() - start) * 1000),
            "msg": f"{result.testsRun} 项测试{'全部通过' if ok else '存在失败'}",
            "next_action": {"type": "CONTINUE" if ok else "STOP", "retryable": False},
        }, ensure_ascii=False))
        sys.exit(0 if ok else 1)
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "code": 1,
            "duration_ms": int((time.monotonic() - start) * 1000),
            "error": "UNEXPECTED_ERROR",
            "msg": f"{type(exc).__name__}: {exc}",
            "next_action": {"type": "STOP", "retryable": False},
        }, ensure_ascii=False))
        sys.exit(1)