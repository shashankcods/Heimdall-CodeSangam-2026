# Benchmarks

Scripts that measure how Heimdall performs, so the README can quote real numbers instead of claims. Run them from the repo root.

| Script | Question it answers |
|---|---|
| `generate_dataset.py` | Do queries stay fast with lots of stored data? Fills a DuckDB file with realistic fake traces and logs (1M or 10M spans). |
| `bench_ingest.py` | How much traffic can Heimdall take? Floods `/v1/traces` and reports spans/sec, response time, and how many 429s come back. |
| `bench_duckdb_vs_sqlite.py` | Was columnar storage the right choice? Runs the same queries on DuckDB and SQLite and compares speed and storage. |

## Running them

**Big dataset for query testing** (no server needed):

```bash
uv run bench/generate_dataset.py --spans 1000000 --db-path bench_heimdall.duckdb
```

Then point the backend at that file (`HEIMDALL_DB_PATH=bench_heimdall.duckdb`) and time the API.

**Ingest throughput** (start the backend first):

```bash
uv run bench/bench_ingest.py --batches 200 --concurrency 10
```

**DuckDB vs SQLite** (no server needed):

```bash
uv run bench/bench_duckdb_vs_sqlite.py --spans 100000
```

## Results

Measured on 100,000 synthetic spans using `uv run bench/bench_duckdb_vs_sqlite.py --spans 100000`:

| Benchmark Metric | DuckDB (Columnar) | SQLite (Row-Store) | Speedup / Efficiency |
|---|---|---|---|
| **Topology Self-Join** | **27.88 ms** | 554.79 ms | **19.9x faster** |
| **Service Error Aggregation** | **3.90 ms** | 67.68 ms | **17.4x faster** |
| **Percentile Math (p50/p95/p99)** | **10.56 ms** | *(slow / custom)* | **Native SIMD execution** |
| **Bulk Write Throughput** | **223,132 spans/s** | 105,432 spans/s | **2.1x faster** |
| **On-Disk Database Size** | **4.62 MB** | 21.22 MB | **4.6x smaller** |

### Key Takeaways

1. **Analytical Self-Joins**: Deriving the service topology graph requires joining child spans to parent spans (`trace_id`, `parent_span_id`, `service`). DuckDB's vectorized join engine only projects the required columns, avoiding full row hydration and completing in under 28 ms.
2. **Columnar Scans**: Aggregating error rates across services (`service`, `status_code`) executes at memory bandwidth speed using SIMD filters, running 17.4x faster than SQLite's row-based B-tree traversal.
3. **Storage Efficiency**: Column-oriented compression (bitpacking, dictionary encoding, and RLE) reduces on-disk storage by **78%** compared to row storage.

