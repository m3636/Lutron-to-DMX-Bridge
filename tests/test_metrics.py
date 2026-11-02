"""Counters behind the status area: rates, latencies, warning counts, driver link stats."""

from __future__ import annotations

import logging

from lutron_to_dmx_bridge.metrics import DriverMetrics, LatencyStats, LogCounter, RateWindow


def test_rate_window_counts_events_inside_the_window_only():
    r = RateWindow(window=1.0)
    for t in (0.0, 0.2, 0.4, 0.6):
        r.add(1, t)
    assert r.rate(0.7) == 4.0
    assert r.rate(1.3) == 2.0  # the events at 0.0 and 0.2 have aged out
    assert r.rate(5.0) == 0.0
    assert r.total == 4  # the running total never ages out


def test_rate_window_add_many_at_once():
    r = RateWindow()
    r.add(94, 0.0)
    assert r.rate(0.1) == 94.0 and r.total == 94


def test_latency_stats_median_p95_and_window():
    s = LatencyStats(window=10.0)
    assert s.snapshot(0.0) == {"last": None, "age_s": None, "median": None, "p95": None, "count": 0}
    for i, ms in enumerate([10, 20, 30, 40, 1000]):
        s.add(ms, now=float(i))
    snap = s.snapshot(5.0)
    assert snap["last"] == 1000 and snap["median"] == 30 and snap["p95"] == 1000 and snap["count"] == 5
    assert snap["age_s"] == 1.0  # the last sample arrived at t=4
    # An hour later nothing is recent, but the last value is still reported.
    old = s.snapshot(3600.0)
    assert old["count"] == 0 and old["median"] is None and old["last"] == 1000 and old["age_s"] > 3000


def test_driver_metrics_reconnect_bookkeeping():
    m = DriverMetrics()
    assert m.snapshot()["connected_for_s"] is None
    m.mark_connected()
    snap = m.snapshot()
    assert snap["connected_for_s"] == 0 and snap["reconnects"] == 0 and snap["retry_in_s"] is None
    m.mark_disconnected()
    m.mark_retry(8.0)
    m.mark_retry(16.0)
    snap = m.snapshot()
    assert snap["connected_for_s"] is None and snap["reconnect_attempts"] == 2
    assert 15 <= snap["retry_in_s"] <= 16
    m.mark_connected()
    snap = m.snapshot()
    assert snap["reconnect_attempts"] == 0 and snap["reconnects"] == 1 and snap["retry_in_s"] is None


def test_log_counter_counts_and_clears():
    counter = LogCounter()
    logger = logging.getLogger("lutron_to_dmx_bridge.test_metrics")
    logger.propagate = False
    logger.addHandler(counter)
    try:
        logger.info("not counted")
        logger.warning("slow %s", "repeater")
        logger.error("send failed")
        logger.error("bad %s")  # a broken format string must not raise
    finally:
        logger.removeHandler(counter)
    snap = counter.snapshot()
    assert snap["warnings"] == 1 and snap["errors"] == 2
    assert snap["recent"][0]["level"] == "E"  # newest first
    assert snap["recent"][-1] == {**snap["recent"][-1], "level": "W", "message": "slow repeater"}
    counter.clear()
    assert counter.snapshot() == {"warnings": 0, "errors": 0, "recent": []}


def test_log_counter_keeps_only_the_latest_lines():
    counter = LogCounter()
    for i in range(50):
        counter.emit(logging.LogRecord("x", logging.WARNING, __file__, 1, f"m{i}", None, None))
    snap = counter.snapshot()
    assert snap["warnings"] == 50 and len(snap["recent"]) == 20 and snap["recent"][0]["message"] == "m49"
