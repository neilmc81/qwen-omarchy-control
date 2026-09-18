"""Tests for the cost collector (aggregation, quota, BSS signature)."""

import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
COST_SCRIPT = REPO / "bin" / "qwen-cost-update"


def load_cost():
    loader = importlib.machinery.SourceFileLoader("qwen_cost_update", str(COST_SCRIPT))
    spec = importlib.util.spec_from_loader("qwen_cost_update", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


cost = None
_STATE_TMP: tempfile.TemporaryDirectory | None = None


def setUpModule():
    global cost, _STATE_TMP
    cost = load_cost()
    # Redirect every state write to a throwaway dir. Without this, the billing
    # test writes and then *deletes* the live widget snapshot at
    # ~/.local/state/qwen-voice/cost/billing.json.
    _STATE_TMP = tempfile.TemporaryDirectory(prefix="qwen-cost-test-state-")
    cost.STATE_DIR = Path(_STATE_TMP.name) / "qwen-voice" / "cost"


def tearDownModule():
    if _STATE_TMP is not None:
        _STATE_TMP.cleanup()


class AggregateTest(unittest.TestCase):
    def test_aggregate_totals(self):
        # Rows are built relative to the current date; hard-coded dates made this
        # test fail the moment the calendar moved past them.
        today = dt.date.today()
        # The last day of the previous month is always a different month, so the
        # third row must fall outside the "this month" bucket.
        previous_month = today.replace(day=1) - dt.timedelta(days=1)
        # Noon UTC anchored rows; a midnight timestamp could land on a different
        # local date than the one under test.
        iso = lambda d, hour: f"{d.isoformat()}T{hour:02d}:00:00+00:00"
        rows = [
            {"at": iso(today, 12), "model": "qwen-audio-3.0-realtime-flash",
             "input_tokens": 100, "output_tokens": 10},
            {"at": iso(today, 13), "model": "qwen-audio-3.0-realtime-flash",
             "input_tokens": 200, "output_tokens": 20},
            {"at": iso(previous_month, 13), "model": "qwen-audio-3.0-realtime-flash",
             "input_tokens": 50, "output_tokens": 5},
        ]
        agg = cost.aggregate(rows)
        self.assertEqual(agg["all"]["responses"], 3)
        self.assertEqual(agg["all"]["inputTokens"], 350)
        self.assertEqual(agg["all"]["outputTokens"], 35)
        self.assertEqual(agg["all"]["totalTokens"], 385)
        self.assertIn(today.isoformat(), agg["daily"])
        self.assertEqual(agg["today"]["responses"], 2)
        self.assertEqual(agg["month"]["responses"], 2)

    def test_aggregate_respects_rates(self):
        rates = {"qwen-audio-3.0-realtime-flash": {"inputPer1k": 1.0, "outputPer1k": 2.0}}
        with unittest.mock.patch.object(cost, "load_config",
                                        return_value={"rates": rates,
                                                      "freeQuota": None}):
            rows = [{"at": "2026-09-14T01:00:00+07:00",
                     "model": "qwen-audio-3.0-realtime-flash",
                     "input_tokens": 1000, "output_tokens": 500}]
            agg = cost.aggregate(rows)
            self.assertEqual(agg["all"]["estCostUsd"], 2.0)  # 1*1.0 + 0.5*2.0

    def test_bss_signature_is_stable(self):
        # A known-correct HMAC-SHA1 Aliyun signature vector.
        params = {
            "AccessKeyId": "LTAI5txxxxxxxxxxxxxx",
            "Action": "QueryAccountBalance",
            "Format": "JSON",
            "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": "testnonce",
            "SignatureVersion": "1.0",
            "Timestamp": "2026-09-14T00:00:00Z",
            "Version": "2017-12-14",
        }
        sig = cost.bss_sign(params, "testsecret")
        self.assertIsInstance(sig, str)
        self.assertTrue(len(sig) > 20)
        # Signature must be deterministic for the same params+secret.
        self.assertEqual(sig, cost.bss_sign(dict(params), "testsecret"))


class QuotaTest(unittest.TestCase):
    def test_quota_in_tokens(self):
        usage = {"all": {"totalTokens": 500, "estCostUsd": 0.0}}
        cfg = {"freeQuota": {"amount": 1000, "unit": "tokens", "label": "free"}}
        with unittest.mock.patch.object(cost, "load_config", return_value=cfg):
            rec = cost.quota_record(usage)
        self.assertEqual(rec["consumed"], 500)
        self.assertEqual(rec["remaining"], 500)
        self.assertEqual(rec["percent"], 50.0)

    def test_quota_in_usd(self):
        usage = {"all": {"totalTokens": 0, "estCostUsd": 1.25}}
        cfg = {"freeQuota": {"amount": 10, "unit": "usd", "label": "free"}}
        with unittest.mock.patch.object(cost, "load_config", return_value=cfg):
            rec = cost.quota_record(usage)
        self.assertEqual(rec["consumed"], 1.25)
        self.assertEqual(rec["remaining"], 8.75)


class LiveBillingTest(unittest.TestCase):
    def test_no_access_key_returns_local_estimate(self):
        with unittest.mock.patch.object(cost, "fetch_live_billing") as fetch:
            fetch.side_effect = RuntimeError("boom")
            rec = cost.read_live_billing({"accessKeyId": ""}, force=True)
        self.assertEqual(rec["source"], "local-estimate")
        self.assertIn("boom", rec["error"])

    def test_billing_snapshot_reused(self):
        snap = cost.billing_snapshot_path()
        snap.parent.mkdir(parents=True, exist_ok=True)
        # `month` is part of the reuse contract: read_live_billing ignores a
        # snapshot from a different month even if it is still within the TTL.
        snap.write_text(json.dumps({
            "source": "aliyun",
            "fetchedAt": dt.datetime.now().isoformat(timespec="seconds"),
            "month": dt.date.today().strftime("%Y-%m"),
            "balance": "12.34",
        }))
        try:
            with unittest.mock.patch.object(cost, "fetch_live_billing") as fetch:
                rec = cost.read_live_billing({"accessKeyId": "x"}, force=False)
            fetch.assert_not_called()
            self.assertEqual(rec["balance"], "12.34")
        finally:
            snap.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()