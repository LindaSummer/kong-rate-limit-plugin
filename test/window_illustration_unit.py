"""Optional illustration regressions; run with the plotter's uv dependencies."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import matplotlib.pyplot as plt

import illustrate_windows as capture
from plot_window_illustration import plot_retries


class RetryTiming(unittest.TestCase):
    def run_client(self, behavior, end_ms, statuses):
        now = 0
        waits = []
        calls = 0

        def wait(target_ms):
            nonlocal now
            waits.append(target_ms)
            now = max(now, round(target_ms * 1_000_000))

        def http(*args, **kwargs):
            nonlocal now, calls
            status = statuses[min(calls, len(statuses) - 1)]
            calls += 1
            now += 100_000_000  # A real response-latency difference must not add to the interval.
            return status, {"X-RateLimit-Mode": "redis", "Retry-After": "3",
                            "X-RateLimit-Time-Ms": str(now // 1_000_000),
                            "X-Kong-Instance": "kong1"}, {}

        with patch.object(capture.time, "time_ns", side_effect=lambda: now), \
             patch.object(capture, "wait_until", side_effect=wait), \
             patch.object(capture, "http", side_effect=http):
            records = capture.retry_client("fixed_window", behavior, 0, end_ms,
                                           run="test", paths={"fixed_window": "/test"}, limit=2)
        return records, waits

    def test_retry_wait_stops_at_capture_deadline(self):
        records, waits = self.run_client("retry-follow", 1000, [200, 200, 429])
        self.assertEqual(len(records), 3)
        self.assertEqual(waits[-1], 1000)
        self.assertLessEqual(max(waits), 1000)

    def test_regular_interval_uses_send_time(self):
        records, _ = self.run_client("retry-ignore", 1000, [200, 200, 429])
        times = [r["sent_ns"] for r in records]
        self.assertEqual(times[:4], [0, 100_000_000, 200_000_000, 400_000_000])
        self.assertTrue(all(b - a == 200_000_000 for a, b in zip(times[2:], times[3:])))

    def test_retry_after_starts_at_response_receipt(self):
        records, _ = self.run_client("retry-follow", 3500, [200, 200, 429, 200])
        self.assertEqual(records[3]["sent_ns"], records[2]["response_ns"] + 3_000_000_000)


class LateArrivals(unittest.TestCase):
    def test_arrival_after_deadline_is_included(self):
        data = {"retry_start_ms": 0, "boundary_ms": 0, "duration_seconds": 2,
                "limit": 20, "window_ms": 10000}
        rows = [{"scenario": "retry-follow", "algorithm": "sliding_window",
                 "timestamp": 1_990_000_000, "upstream_timestamp": 2_100_000_000,
                 "return_code": 200}]
        with tempfile.TemporaryDirectory() as directory, patch("plot_window_illustration.plt.close"):
            try:
                plot_retries(data, rows, Path(directory))
                self.assertGreaterEqual(plt.gcf().axes[0].get_xlim()[1], 2.1)
                self.assertTrue((Path(directory) / "retry-behavior.png").is_file())
            finally:
                plt.close.assert_called_once()
        plt.close("all")


if __name__ == "__main__":
    unittest.main()
