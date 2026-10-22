import json

from heimdall.store.schema import connect


def test_connect_creates_tables(tmp_path):
    conn = connect(str(tmp_path / "test.duckdb"))
    tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
    assert {"spans", "logs"} <= tables


def test_connect_is_idempotent(tmp_path):
    db = str(tmp_path / "test.duckdb")
    connect(db).close()
    connect(db).close()  # second run must not fail on existing tables


def test_env_var_sets_db_path(tmp_path, monkeypatch):
    db = tmp_path / "from_env.duckdb"
    monkeypatch.setenv("HEIMDALL_DB_PATH", str(db))
    connect().close()
    assert db.exists()


def test_span_round_trip(tmp_path):
    conn = connect(str(tmp_path / "test.duckdb"))
    start, end = 1_729_580_000_000_000_000, 1_729_580_002_400_000_000
    conn.execute(
        "INSERT INTO spans VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "4bf92f3577b34da6a3ce929d0e0e4736",
            "00f067aa0ba902b7",
            "gateway",
            "POST /order",
            "SERVER",
            start,
            end,
            end - start,
            "ERROR",
            "upstream timeout",
            500,
            "/order",
            json.dumps({"http.method": "POST"}),
        ],
    )
    row = conn.execute(
        "SELECT service, duration_ns, http_status, attributes->>'$.\"http.method\"' FROM spans"
    ).fetchone()
    assert row == ("gateway", 2_400_000_000, 500, "POST")
