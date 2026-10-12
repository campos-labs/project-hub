from __future__ import annotations

import argparse
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

from build_layout_catalog import compile_layout_catalog
from poc_analysis_catalog_execution import build_schema_columns, ensure_relation_table
from poc_parsing_layout import coerce_value, load_layout_metadata, parse_sped


WORKLOAD = {
    "profile_counts": "SELECT 'c100' AS code, COUNT(*) AS cnt FROM c100 UNION ALL SELECT 'c190', COUNT(*) FROM c190",
    "join_c100_c190": (
        "SELECT c100.cod_part, COUNT(*) AS lines "
        "FROM c100 JOIN c190 ON c100.record_id = c190.parent_record_id "
        "GROUP BY c100.cod_part ORDER BY lines DESC, c100.cod_part LIMIT 20"
    ),
    "movement_cfop": (
        "SELECT c190.cfop, SUM(c190.vl_opr) AS total_operacao "
        "FROM c100 JOIN c190 ON c100.record_id = c190.parent_record_id "
        "WHERE c100.ind_oper='1' AND c100.cod_sit IN ('00','01') "
        "GROUP BY c190.cfop ORDER BY total_operacao DESC, c190.cfop LIMIT 20"
    ),
}


@dataclass
class StrategyResult:
    strategy: str
    prepare_times: list[float]
    first_query_times: list[float]
    repeat_query_times: list[float]
    restore_times: list[float]
    storage_bytes: int
    workload_result: dict[str, list[tuple[Any, ...]]]


def median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def execute_workload(conn: duckdb.DuckDBPyConnection) -> dict[str, list[tuple[Any, ...]]]:
    return {name: conn.execute(sql).fetchall() for name, sql in WORKLOAD.items()}


def register_parquet_views(conn: duckdb.DuckDBPyConnection, parquet_dir: Path) -> None:
    conn.execute(f"CREATE VIEW c100 AS SELECT * FROM read_parquet('{(parquet_dir / 'c100.parquet').as_posix()}')")
    conn.execute(f"CREATE VIEW c190 AS SELECT * FROM read_parquet('{(parquet_dir / 'c190.parquet').as_posix()}')")


def run_queries(conn: duckdb.DuckDBPyConnection) -> tuple[float, float, dict[str, list[tuple[Any, ...]]]]:
    start = time.perf_counter()
    first_result = execute_workload(conn)
    first = time.perf_counter() - start

    rep_start = time.perf_counter()
    repeat_result = execute_workload(conn)
    repeat = time.perf_counter() - rep_start

    if first_result != repeat_result:
        raise RuntimeError("Workload repetido retornou resultado diferente.")

    return first, repeat, first_result


def prepare_from_parsed(
    conn: duckdb.DuckDBPyConnection,
    parsed: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    materialize_selected_relations(conn, parsed, metadata, allowed_record_codes={"C100", "C190"})


def materialize_selected_relations(
    conn: duckdb.DuckDBPyConnection,
    parsed: dict[str, Any],
    metadata: dict[str, Any],
    allowed_record_codes: set[str],
) -> dict[str, dict[str, Any]]:
    records = parsed["records"]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        if rec["record_code"] in allowed_record_codes:
            grouped.setdefault(rec["record_code"], []).append(rec)

    relation_info: dict[str, dict[str, Any]] = {}
    for record_code in sorted(allowed_record_codes):
        meta = metadata.get(record_code)
        if meta is None:
            raise RuntimeError(f"Relacao obrigatoria ausente no leiaute: {record_code}")

        table_name = record_code.lower()
        schema_cols = build_schema_columns(record_code, meta.fields)
        ensure_relation_table(conn, table_name, schema_cols)

        recs = grouped.get(record_code, [])
        if recs:
            cols = [c for c, _ in schema_cols]
            placeholders = ", ".join(["?"] * len(cols))
            quoted_cols = ", ".join(f'"{c}"' for c in cols)
            insert_sql = f'INSERT INTO "{table_name}" ({quoted_cols}) VALUES ({placeholders})'

            values_batch: list[tuple[Any, ...]] = []
            for rec in recs:
                base = {
                    "file_id": rec["file_id"],
                    "record_id": rec["record_id"],
                    "record_code": rec["record_code"],
                    "record_level": rec["record_level"],
                    "source_line": rec["source_line"],
                    "parent_record_id": rec["parent_record_id"],
                    "parent_record_code": rec["parent_record_code"],
                }
                for f in meta.fields:
                    key = f.field_code.lower()
                    raw = rec["values"].get(f.field_code, "")
                    base[key] = coerce_value(raw, f.field_type)

                values_batch.append(tuple(base.get(c) for c in cols))

            conn.executemany(insert_sql, values_batch)

        relation_info[record_code] = {"table": table_name, "rows": len(recs)}

    return relation_info


def benchmark_txt_on_demand(sped_path: Path, layout_catalog: Path, runs: int) -> StrategyResult:
    prepare_times: list[float] = []
    first_times: list[float] = []
    repeat_times: list[float] = []
    restore_times: list[float] = []
    workload_result: dict[str, list[tuple[Any, ...]]] = {}

    for _ in range(runs):
        t0 = time.perf_counter()
        metadata = load_layout_metadata(str(layout_catalog), sped_type="EFD-ICMS-IPI", layout_version="020")
        parsed = parse_sped(str(sped_path), metadata)
        conn = duckdb.connect(database=":memory:")
        prepare_from_parsed(conn, parsed, metadata)
        prepare_times.append(time.perf_counter() - t0)

        first, repeat, workload_result = run_queries(conn)
        first_times.append(first)
        repeat_times.append(repeat)

        r0 = time.perf_counter()
        conn.close()
        metadata2 = load_layout_metadata(str(layout_catalog), sped_type="EFD-ICMS-IPI", layout_version="020")
        parsed2 = parse_sped(str(sped_path), metadata2)
        conn2 = duckdb.connect(database=":memory:")
        prepare_from_parsed(conn2, parsed2, metadata2)
        restore_result = execute_workload(conn2)
        if restore_result != workload_result:
            raise RuntimeError("Restauracao TXT retornou resultado diferente do workload base.")
        conn2.close()
        restore_times.append(time.perf_counter() - r0)

    return StrategyResult(
        strategy="txt_on_demand",
        prepare_times=prepare_times,
        first_query_times=first_times,
        repeat_query_times=repeat_times,
        restore_times=restore_times,
        storage_bytes=0,
        workload_result=workload_result,
    )


def benchmark_duckdb_materialized(
    sped_path: Path,
    layout_catalog: Path,
    workspace_db: Path,
    runs: int,
) -> StrategyResult:
    prepare_times: list[float] = []
    first_times: list[float] = []
    repeat_times: list[float] = []
    restore_times: list[float] = []
    workload_result: dict[str, list[tuple[Any, ...]]] = {}

    for _ in range(runs):
        if workspace_db.exists():
            workspace_db.unlink()

        t0 = time.perf_counter()
        metadata = load_layout_metadata(str(layout_catalog), sped_type="EFD-ICMS-IPI", layout_version="020")
        parsed = parse_sped(str(sped_path), metadata)
        conn = duckdb.connect(str(workspace_db))
        try:
            prepare_from_parsed(conn, parsed, metadata)
        finally:
            conn.close()
        prepare_times.append(time.perf_counter() - t0)

        # Em Windows, reabrir o mesmo arquivo com read_only pode falhar por lock residual
        # mesmo no mesmo processo. Abrir em modo padrao evita erro espurio no benchmark.
        conn_run = duckdb.connect(str(workspace_db))
        try:
            first, repeat, workload_result = run_queries(conn_run)
        finally:
            conn_run.close()
        first_times.append(first)
        repeat_times.append(repeat)

        r0 = time.perf_counter()
        conn_restore = duckdb.connect(str(workspace_db))
        try:
                restore_result = execute_workload(conn_restore)
                if restore_result != workload_result:
                    raise RuntimeError("Restauracao DuckDB retornou resultado diferente do workload base.")
        finally:
            conn_restore.close()
        restore_times.append(time.perf_counter() - r0)

    storage = workspace_db.stat().st_size if workspace_db.exists() else 0
    return StrategyResult(
        strategy="duckdb_materialized",
        prepare_times=prepare_times,
        first_query_times=first_times,
        repeat_query_times=repeat_times,
        restore_times=restore_times,
        storage_bytes=storage,
        workload_result=workload_result,
    )


def benchmark_parquet(
    sped_path: Path,
    layout_catalog: Path,
    parquet_dir: Path,
    runs: int,
) -> StrategyResult:
    prepare_times: list[float] = []
    first_times: list[float] = []
    repeat_times: list[float] = []
    restore_times: list[float] = []
    workload_result: dict[str, list[tuple[Any, ...]]] = {}

    for _ in range(runs):
        if parquet_dir.exists():
            for f in parquet_dir.glob("*.parquet"):
                f.unlink()
        parquet_dir.mkdir(parents=True, exist_ok=True)

        t0 = time.perf_counter()
        metadata = load_layout_metadata(str(layout_catalog), sped_type="EFD-ICMS-IPI", layout_version="020")
        parsed = parse_sped(str(sped_path), metadata)
        conn_stage = duckdb.connect(database=":memory:")
        prepare_from_parsed(conn_stage, parsed, metadata)
        conn_stage.execute(f"COPY c100 TO '{(parquet_dir / 'c100.parquet').as_posix()}' (FORMAT PARQUET)")
        conn_stage.execute(f"COPY c190 TO '{(parquet_dir / 'c190.parquet').as_posix()}' (FORMAT PARQUET)")
        conn_stage.close()
        prepare_times.append(time.perf_counter() - t0)

        conn = duckdb.connect(database=":memory:")
        register_parquet_views(conn, parquet_dir)
        first, repeat, workload_result = run_queries(conn)
        conn.close()
        first_times.append(first)
        repeat_times.append(repeat)

        r0 = time.perf_counter()
        conn_restore = duckdb.connect(database=":memory:")
        register_parquet_views(conn_restore, parquet_dir)
        restore_result = execute_workload(conn_restore)
        if restore_result != workload_result:
            raise RuntimeError("Restauracao Parquet retornou resultado diferente do workload base.")
        conn_restore.close()
        restore_times.append(time.perf_counter() - r0)

    storage = sum(f.stat().st_size for f in parquet_dir.glob("*.parquet"))
    return StrategyResult(
        strategy="parquet_by_record",
        prepare_times=prepare_times,
        first_query_times=first_times,
        repeat_query_times=repeat_times,
        restore_times=restore_times,
        storage_bytes=storage,
        workload_result=workload_result,
    )


def print_summary(results: list[StrategyResult]) -> None:
    print("\n=== SPIKE STAGING SUMMARY ===")
    print("strategy,prepare_median_s,first_query_median_s,repeat_query_median_s,restore_median_s,storage_bytes")
    for r in results:
        print(
            f"{r.strategy},{median(r.prepare_times):.6f},{median(r.first_query_times):.6f},"
            f"{median(r.repeat_query_times):.6f},{median(r.restore_times):.6f},{r.storage_bytes}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Spike 1 - Benchmark de estrategias de staging.")
    parser.add_argument(
        "--input",
        default="rfcs/agente-sped-fiscal/research/data/sped_icms_analysis_reference.txt",
        help="Arquivo SPED de entrada.",
    )
    parser.add_argument(
        "--layout-source",
        default="rfcs/agente-sped-fiscal/research/data/sped_layout_fields.csv",
        help="CSV do leiaute.",
    )
    parser.add_argument("--layout-catalog", default="layout_catalog.duckdb", help="Catalogo de leiaute.")
    parser.add_argument("--workspace-db", default="workspace.duckdb", help="Arquivo DuckDB materializado.")
    parser.add_argument("--parquet-dir", default="parquet_workspace", help="Diretorio parquet.")
    parser.add_argument("--runs", type=int, default=3, help="Numero de repeticoes por estrategia.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.runs < 1:
        raise ValueError("--runs deve ser maior ou igual a 1.")

    input_path = Path(args.input).resolve()
    layout_source = Path(args.layout_source).resolve()
    layout_catalog = Path(args.layout_catalog).resolve()
    workspace_db = Path(args.workspace_db).resolve()
    parquet_dir = Path(args.parquet_dir).resolve()

    if not input_path.exists():
        raise FileNotFoundError(
            f"Arquivo de entrada nao encontrado: {input_path}. Gere antes com build_sped_benchmark.py"
        )

    if not layout_source.exists():
        raise FileNotFoundError(f"CSV de leiaute nao encontrado: {layout_source}")

    compile_layout_catalog(layout_source, layout_catalog, layout_version="020")

    results = [
        benchmark_txt_on_demand(input_path, layout_catalog, args.runs),
        benchmark_duckdb_materialized(input_path, layout_catalog, workspace_db, args.runs),
        benchmark_parquet(input_path, layout_catalog, parquet_dir, args.runs),
    ]

    reference = results[0].workload_result
    for result in results[1:]:
        if result.workload_result != reference:
            raise RuntimeError(f"Resultado funcional divergente: {result.strategy}")

    print_summary(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
