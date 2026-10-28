import itertools

import pytest

from heimdall.ingest.models import Span
from heimdall.queries.anomalies import find_anomalies
from heimdall.store.schema import connect

MS = 1_000_000
NOW_MS = 10_000_000  # fixed "now" so the windows are deterministic
MIN = 60_000  # ms per minute
_ids = itertools.count(1)


def spans(service, name, count, dur_ms, minutes_ago, errors=0, trace_ids=None):
    """`count` spans of one operation, spread over one minute starting `minutes_ago` before now."""
    out = []
    for i in range(count):
        n = next(_ids)
        start_ms = NOW_MS - minutes_ago * MIN + i * (MIN // max(count, 1))
        out.append(Span(
            trace_id=(trace_ids[i] if trace_ids else f"{n:032x}"), span_id=f"{n:016x}", service=service,
            name=name, kind="SERVER", start_time=start_ms * MS, end_time=(start_ms + dur_ms) * MS,
            duration_ns=dur_ms * MS, status_code="ERROR" if i < errors else "OK",
        ))
    return out


def insert(conn, rows):
    for s in rows:
        row = s.to_row()
        conn.execute(f"INSERT INTO spans VALUES ({', '.join('?' * len(row))})", list(row.values()))


@pytest.fixture
def cur(tmp_path):
    return connect(str(tmp_path / "test.duckdb")).cursor()


def detect(cur, **kwargs):
    return find_anomalies(cur, now_ms=NOW_MS, **kwargs)["anomalies"]


def test_flags_latency_spike_during_error_spike(cur):
    insert(cur, spans("payment-service", "chargeCard", 30, 25, minutes_ago=30)        # baseline: 25 ms, no errors
           + spans("payment-service", "chargeCard", 20, 1900, minutes_ago=3, errors=20))  # recent: 1.9 s, all failing
    [a] = detect(cur)
    assert (a["service"], a["name"]) == ("payment-service", "chargeCard")
    assert (a["recent_p99_ms"], a["baseline_p99_ms"]) == (1900.0, 25.0)
    assert a["p99_ratio"] == pytest.approx(76.0)
    assert a["error_spike"] is True
    assert (a["recent_error_rate"], a["baseline_error_rate"]) == (1.0, 0.0)
    assert (a["recent_count"], a["baseline_count"]) == (20, 30)


def test_stable_operation_is_not_flagged(cur):
    insert(cur, spans("gateway", "POST /order", 30, 400, minutes_ago=30)
           + spans("gateway", "POST /order", 20, 420, minutes_ago=3))
    assert detect(cur) == []


def test_latency_spike_without_errors_is_flagged_but_ranked_after_error_spikes(cur):
    insert(cur,
           spans("search", "query", 30, 50, minutes_ago=30) + spans("search", "query", 20, 5000, minutes_ago=3)
           + spans("payment-service", "chargeCard", 30, 25, minutes_ago=30)
           + spans("payment-service", "chargeCard", 20, 200, minutes_ago=3, errors=10))
    result = detect(cur)
    # search jumped 100x but without errors; chargeCard jumped 8x during an error spike -> listed first
    assert [(a["service"], a["error_spike"]) for a in result] == [("payment-service", True), ("search", False)]


def test_tiny_absolute_increase_is_not_flagged(cur):
    insert(cur, spans("cache", "GET", 30, 1, minutes_ago=30) + spans("cache", "GET", 20, 3, minutes_ago=3))
    assert detect(cur) == []  # 3x, but only 2 ms slower


def test_too_few_samples_gives_no_verdict(cur):
    insert(cur, spans("payment-service", "chargeCard", 30, 25, minutes_ago=30)
           + spans("payment-service", "chargeCard", 5, 1900, minutes_ago=3, errors=5))
    assert detect(cur) == []
    assert len(detect(cur, min_samples=5)) == 1


def test_error_spike_without_latency_change_is_not_a_latency_anomaly(cur):
    insert(cur, spans("payment-service", "chargeCard", 30, 25, minutes_ago=30)
           + spans("payment-service", "chargeCard", 20, 26, minutes_ago=3, errors=20))
    assert detect(cur) == []


def test_data_older_than_the_baseline_is_ignored(cur):
    insert(cur, spans("payment-service", "chargeCard", 30, 2000, minutes_ago=120)   # slow, but 2 hours ago
           + spans("payment-service", "chargeCard", 30, 25, minutes_ago=30)
           + spans("payment-service", "chargeCard", 20, 25, minutes_ago=3))
    assert detect(cur) == []


def test_zero_duration_baseline_does_not_divide_by_zero(cur):
    insert(cur, spans("gateway", "http send", 30, 0, minutes_ago=30) + spans("gateway", "http send", 20, 0, minutes_ago=3))
    assert detect(cur) == []


def test_duplicates_are_counted_once(cur):
    rows = spans("payment-service", "chargeCard", 30, 25, minutes_ago=30) \
        + spans("payment-service", "chargeCard", 20, 1900, minutes_ago=3, errors=20)
    insert(cur, rows + rows)  # every span delivered twice
    [a] = detect(cur)
    assert (a["recent_count"], a["baseline_count"]) == (20, 30)


def test_example_trace_is_the_slowest_recent_one(cur):
    recent = spans("payment-service", "chargeCard", 20, 300, minutes_ago=3, errors=20)
    recent[7].duration_ns = recent[7].end_time - recent[7].start_time + 1500 * MS  # one much slower call
    recent[7].end_time += 1500 * MS
    insert(cur, spans("payment-service", "chargeCard", 30, 25, minutes_ago=30) + recent)
    [a] = detect(cur)
    assert a["example_trace_id"] == recent[7].trace_id


def test_windows_and_thresholds_are_configurable(cur):
    insert(cur, spans("payment-service", "chargeCard", 30, 25, minutes_ago=30)
           + spans("payment-service", "chargeCard", 20, 60, minutes_ago=3))
    assert len(detect(cur)) == 1                   # 2.4x and +35 ms: flagged with the defaults
    assert detect(cur, min_ratio=3.0) == []        # not a 3x jump
    assert detect(cur, min_increase_ms=50) == []   # not 50 ms slower
    result = find_anomalies(cur, now_ms=NOW_MS, recent_minutes=10, baseline_minutes=30)
    assert result["recent_window"] == {"from_ms": NOW_MS - 10 * MIN, "to_ms": NOW_MS}
    assert result["baseline_window"] == {"from_ms": NOW_MS - 40 * MIN, "to_ms": NOW_MS - 10 * MIN}
