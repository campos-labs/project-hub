from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import yaml

from build_layout_catalog import compile_layout_catalog
from poc_parsing_layout import load_layout_metadata, parse_sped


BLOCKED_PATTERN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|copy|pragma|install|load|call|export)\b",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class CatalogContext:
    catalog: dict[str, Any]
    analyses: dict[str, dict[str, Any]]


def parse_br_decimal(value: str) -> Decimal | None:
    value = value.strip()
    if not value:
        return None
    normalized = value.replace(".", "").replace(",", ".")
    try:
        return Decimal(normalized)
    except Exception:
        return None


def parse_br_date(value: str) -> date | None:
    value = value.strip()
    if len(value) != 8 or not value.isdigit():
        return None
    return date(int(value[4:8]), int(value[2:4]), int(value[0:2]))


def load_catalog(catalog_path: Path) -> CatalogContext:
    data = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Catalogo YAML invalido: raiz nao eh objeto.")

    analyses = data.get("analyses", {})
    if not isinstance(analyses, dict) or not analyses:
        raise ValueError("Catalogo YAML invalido: analyses ausente ou vazio.")

    known_top = {
        "schema_version",
        "catalog_version",
        "product_release",
        "catalog_id",
        "language",
        "sped",
        "loader",
        "runtime",
        "defaults",
        "parameter_definitions",
        "reference_validation",
        "analyses",
    }
    unknown = sorted(set(data.keys()) - known_top)
    if unknown:
        raise ValueError(f"Catalogo contem chaves desconhecidas no topo: {unknown}")

    return CatalogContext(catalog=data, analyses=analyses)


def coerce_value(raw: str, field_type: str) -> Any:
    if raw is None:
        return None
    raw = raw.strip()
    if raw == "":
        return None

    ftype = field_type.lower()
    if ftype in {"float", "double", "decimal"}:
        return parse_br_decimal(raw)
    if ftype == "date":
        return parse_br_date(raw)
    if ftype in {"int", "integer", "bigint"}:
        try:
            return int(raw)
        except ValueError:
            return None
    return raw


def normalize_identifier(name: str) -> str:
    return name.lower().replace("-", "_")


def ensure_relation_table(conn: duckdb.DuckDBPyConnection, table_name: str, columns: list[tuple[str, str]]) -> None:
    quoted_table = f'"{table_name}"'
    cols_sql = ", ".join(f'"{c}" {t}' for c, t in columns)
    conn.execute(f"CREATE TABLE IF NOT EXISTS {quoted_table} ({cols_sql})")


def build_schema_columns(record_code: str, meta_fields: list[Any]) -> list[tuple[str, str]]:
    columns = [
        ("file_id", "VARCHAR"),
        ("record_id", "BIGINT"),
        ("record_code", "VARCHAR"),
        ("record_level", "INTEGER"),
        ("source_line", "BIGINT"),
        ("parent_record_id", "BIGINT"),
        ("parent_record_code", "VARCHAR"),
    ]
    seen: set[str] = {c for c, _ in columns}
    for f in meta_fields:
        base_field = normalize_identifier(f.field_code)
        field = base_field
        suffix = 2
        while field in seen:
            field = f"{base_field}__{suffix}"
            suffix += 1
        seen.add(field)

        field_type = f.field_type.lower()
        if field_type == "date":
            sql_type = "DATE"
        elif field_type in {"float", "double", "decimal"}:
            sql_type = "DECIMAL(18, 6)"
        elif field_type in {"int", "integer", "bigint"}:
            sql_type = "BIGINT"
        else:
            sql_type = "VARCHAR"
        columns.append((field, sql_type))
    return columns


def materialize_relations(
    conn: duckdb.DuckDBPyConnection,
    parsed: dict[str, Any],
    layout_metadata: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    records = parsed["records"]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        grouped.setdefault(rec["record_code"], []).append(rec)

    relation_info: dict[str, dict[str, Any]] = {}

    for record_code, meta in layout_metadata.items():
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
                    key = normalize_identifier(f.field_code)
                    raw = rec["values"].get(f.field_code, "")
                    base[key] = coerce_value(raw, f.field_type)

                values_batch.append(tuple(base.get(c) for c in cols))

            conn.executemany(insert_sql, values_batch)

        relation_info[record_code] = {
            "table": table_name,
            "rows": len(recs),
            "empty_relation": len(recs) == 0,
            "columns": [c for c, _ in schema_cols],
        }

    return relation_info


def compute_effective_parameters(file_profile: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    declared = analysis.get("parameters", {})
    for pname, pmeta in declared.items():
        default = pmeta.get("default")
        default_from = pmeta.get("default_from")
        value = default
        if default_from == "file_profile.period_start":
            value = file_profile.get("period_start")
        elif default_from == "file_profile.period_end":
            value = file_profile.get("period_end")

        if value is None and pmeta.get("required", False):
            raise ValueError(f"Parametro obrigatorio sem valor: {pname}")
        params[pname] = value

    return params


def validate_availability(analysis: dict[str, Any], relation_info: dict[str, dict[str, Any]]) -> tuple[bool, str]:
    availability = analysis.get("availability", {})
    req_records = availability.get("required_records", [])
    req_fields = availability.get("required_fields", {})
    presence_required = availability.get("record_presence_required", True)

    for rec in req_records:
        info = relation_info.get(rec)
        if info is None:
            return False, f"Registro requerido ausente no leiaute: {rec}"
        if presence_required and info.get("rows", 0) == 0:
            return False, f"Registro requerido sem ocorrencia no arquivo: {rec}"

    for rec_code, fields in req_fields.items():
        info = relation_info.get(rec_code)
        if not info:
            return False, f"Registro requerido ausente: {rec_code}"
        cols = set(info["columns"])
        for f in fields:
            if normalize_identifier(f) not in cols:
                return False, f"Campo requerido ausente: {rec_code}.{f}"

    return True, ""


def assert_sql_blocked(sql: str) -> None:
    try:
        ensure_sql_allowed(sql)
    except ValueError as exc:
        print(f"BLOCKED {sql} -> {exc}")
        return

    raise RuntimeError(f"self_check_sql_guard: rejeicao esperada nao ocorreu para {sql}")


def empty_masked_payload() -> dict[str, Any]:
    return {
        "rows": [],
        "total_rows": 0,
        "returned_rows": 0,
        "truncated": False,
    }


def bounded_int(value: Any, *, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} deve ser inteiro.") from exc

    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} deve estar entre {minimum} e {maximum}: {parsed}")

    return parsed


def ensure_sql_allowed(sql: str) -> None:
    stripped = sql.strip()
    if not stripped:
        raise ValueError("SQL vazio no catalogo.")

    if BLOCKED_PATTERN.search(stripped):
        raise ValueError("SQL bloqueado por conter operacao nao permitida.")

    body = stripped.rstrip(";").strip()
    if ";" in body:
        raise ValueError("Apenas uma instrucao SQL eh permitida.")

    upper = body.upper()
    if not (upper.startswith("SELECT") or upper.startswith("WITH")):
        raise ValueError("Raiz SQL nao permitida; apenas SELECT/WITH.")


def mask_value(value: Any) -> str:
    text = "" if value is None else str(value)
    if len(text) <= 4:
        return "***"
    return f"{text[:2]}***{text[-2:]}"


def normalize_scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        return Decimal(str(value))
    if isinstance(value, date):
        return value.isoformat()
    return value


def compare_expected_to_actual(expected: Any, actual: Any, path: str = "") -> list[str]:
    issues: list[str] = []

    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: esperado dict, obtido {type(actual).__name__}"]
        for key, exp_value in expected.items():
            if key not in actual:
                issues.append(f"{path}.{key}: chave ausente")
                continue
            issues.extend(compare_expected_to_actual(exp_value, actual[key], f"{path}.{key}" if path else key))
        return issues

    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{path}: esperado list, obtido {type(actual).__name__}"]
        if len(expected) != len(actual):
            issues.append(f"{path}: tamanho esperado {len(expected)} obtido {len(actual)}")
            return issues
        for idx, (exp_item, act_item) in enumerate(zip(expected, actual)):
            issues.extend(compare_expected_to_actual(exp_item, act_item, f"{path}[{idx}]") )
        return issues

    exp_norm = normalize_scalar(expected)
    act_norm = normalize_scalar(actual)
    if isinstance(exp_norm, (int, float, Decimal)) or isinstance(act_norm, (int, float, Decimal)):
        try:
            if Decimal(str(exp_norm)) != Decimal(str(act_norm)):
                issues.append(f"{path}: esperado {exp_norm} obtido {act_norm}")
        except Exception:
            if exp_norm != act_norm:
                issues.append(f"{path}: esperado {exp_norm} obtido {act_norm}")
        return issues

    if exp_norm != act_norm:
        issues.append(f"{path}: esperado {exp_norm} obtido {act_norm}")
    return issues


def actual_result_view(analysis_id: str, result: dict[str, Any]) -> dict[str, Any]:
    rows = result.get("rows", [])
    view: dict[str, Any] = {"row_count": result.get("row_count", 0)}

    if "source_row_count" in result:
        view["source_row_count"] = result.get("source_row_count", 0)
    if "source_records_present" in result:
        view["source_records_present"] = result.get("source_records_present", False)

    if analysis_id == "movement_by_cfop" and rows:
        view["total_operations"] = rows[0].get("total_operacoes")
        view["displayed_subtotal"] = rows[0].get("displayed_subtotal")
        view["top_cfops"] = [{"cfop": row.get("cfop"), "total": row.get("total_operacao")} for row in rows]
    elif analysis_id == "cancelled_documents":
        view["document_numbers"] = [row.get("num_doc") for row in rows]
    elif analysis_id == "block_k_activity" and rows:
        view["total_distinct_orders"] = rows[0].get("total_distinct_orders")
        view["daily_distinct_orders"] = {
            str(row.get("dt_ini_op")): row.get("distinct_order_count") for row in rows
        }
    elif analysis_id == "supplier_concentration" and rows:
        first = rows[0]
        view["supplier"] = first.get("cod_part")
        view["supplier_total"] = first.get("supplier_total")
        view["total_purchases"] = first.get("total_purchases")
    elif analysis_id == "possible_duplicate_documents" and rows:
        first = rows[0]
        view["participant"] = first.get("cod_part")
        view["series"] = first.get("ser")
        view["document_number"] = first.get("num_doc")
        view["occurrence_count"] = first.get("occurrence_count")
    elif analysis_id == "document_sequence_gaps":
        view["gaps"] = [
            {
                "series": row.get("ser"),
                "first_missing_number": row.get("first_missing_number"),
                "last_missing_number": row.get("last_missing_number"),
            }
            for row in rows
        ]
    elif analysis_id == "generic_ncm_high_value" and rows:
        first = rows[0]
        view["item_code"] = first.get("cod_item")
        view["ncm"] = first.get("cod_ncm")
        view["item_value"] = first.get("vl_item")
    elif analysis_id == "possible_credit_on_consumption_cfop":
        view["cfops"] = sorted([row.get("cfop") for row in rows if row.get("cfop") is not None])
        view["total_icms"] = sum((row.get("vl_icms") or 0) for row in rows)

    return view


def execute_analysis(
    conn: duckdb.DuckDBPyConnection,
    analysis_id: str,
    analysis: dict[str, Any],
    file_profile: dict[str, Any],
    relation_info: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    available, reason = validate_availability(analysis, relation_info)
    source_records = analysis.get("availability", {}).get("required_records", [])
    source_row_count = sum(relation_info.get(rec, {}).get("rows", 0) for rec in source_records)
    source_records_present = all(relation_info.get(rec, {}).get("rows", 0) > 0 for rec in source_records)
    if not available:
        return {
            "analysis_id": analysis_id,
            "status": "unavailable",
            "reason": reason,
            "rows": [],
            "masked_payload": empty_masked_payload(),
            "row_count": 0,
            "source_row_count": source_row_count,
            "source_records_present": source_records_present,
        }

    sql = analysis["execution"]["sql"]
    ensure_sql_allowed(sql)

    params = compute_effective_parameters(file_profile=file_profile, analysis=analysis)
    all_params = {
        "start_date": params.get("start_date"),
        "end_date": params.get("end_date"),
        "top_n": params.get("top_n", 3),
        "threshold_percent": params.get("threshold_percent", 50.0),
        "max_gap_size": params.get("max_gap_size", 20),
        "min_item_value": params.get("min_item_value", Decimal("10000.00")),
    }

    named_params = set(re.findall(r"[:$]([A-Za-z_][A-Za-z0-9_]*)", sql))
    if named_params:
        sql_params = {k: v for k, v in all_params.items() if k in named_params}
    else:
        sql_params = all_params

    row_limit = bounded_int(
        analysis.get("result", {}).get("row_limit", 5000),
        name="result.row_limit",
        minimum=1,
        maximum=5000,
    )
    wrapped = f"SELECT * FROM ({sql}) AS subq LIMIT {row_limit}"
    narrative_input = analysis.get("narrative", {}).get("input", {})
    narrative_max_rows = bounded_int(
        narrative_input.get("max_rows", 20),
        name="narrative.input.max_rows",
        minimum=0,
        maximum=min(row_limit, 100),
    )

    try:
        cursor = conn.execute(wrapped, sql_params)
        rows = cursor.fetchall()
        columns = [d[0] for d in cursor.description]
        result_rows = [dict(zip(columns, r)) for r in rows]
    except Exception as exc:
        return {
            "analysis_id": analysis_id,
            "status": "error",
            "reason": f"Erro controlado de execução: {exc}",
            "rows": [],
            "masked_payload": empty_masked_payload(),
            "row_count": 0,
            "source_row_count": source_row_count,
            "source_records_present": source_records_present,
        }

    sensitive = {normalize_identifier(c) for c in analysis.get("privacy", {}).get("sensitive_columns", [])}
    masked_rows = []
    for row in result_rows:
        masked = {}
        for k, v in row.items():
            masked[k] = mask_value(v) if normalize_identifier(k) in sensitive else v
        masked_rows.append(masked)

    narrative_rows = masked_rows[:narrative_max_rows]

    return {
        "analysis_id": analysis_id,
        "status": "ok",
        "reason": "",
        "row_count": len(result_rows),
        "rows": result_rows,
        "masked_payload": {
            "rows": narrative_rows,
            "total_rows": len(masked_rows),
            "returned_rows": len(narrative_rows),
            "truncated": len(masked_rows) > narrative_max_rows,
        },
        "source_row_count": source_row_count,
        "source_records_present": source_records_present,
    }


def validate_reference_expectations(results: dict[str, dict[str, Any]], expected: dict[str, Any]) -> list[str]:
    issues: list[str] = []

    for aid, exp in expected.items():
        got = actual_result_view(aid, results.get(aid, {}))
        issues.extend(compare_expected_to_actual(exp, got, aid))

    return issues


def assert_masking_contract(results: dict[str, dict[str, Any]], analyses: dict[str, dict[str, Any]]) -> None:
    checked = False

    for analysis_id, result in results.items():
        if result.get("status") != "ok":
            continue

        sensitive = {
            normalize_identifier(column)
            for column in analyses[analysis_id].get("privacy", {}).get("sensitive_columns", [])
        }

        raw_rows = result.get("rows", [])
        narrative_rows = result.get("masked_payload", {}).get("rows", [])

        if not sensitive or not raw_rows or not narrative_rows:
            continue

        raw_row = raw_rows[0]
        masked_row = narrative_rows[0]

        for column in sensitive:
            if column not in raw_row:
                continue
            if masked_row.get(column) == raw_row.get(column):
                raise RuntimeError(f"self_check_masking: coluna nao mascarada: {analysis_id}.{column}")
            checked = True

    if not checked:
        raise RuntimeError("self_check_masking: nenhuma coluna sensivel pôde ser validada.")

    print("self_check_masking: ok")


def assert_payload_contract(results: dict[str, dict[str, Any]]) -> None:
    required_keys = {"rows", "total_rows", "returned_rows", "truncated"}

    for analysis_id, result in results.items():
        payload = result.get("masked_payload")
        if not isinstance(payload, dict):
            raise RuntimeError(f"{analysis_id}: masked_payload deve ser objeto.")

        if set(payload) != required_keys:
            raise RuntimeError(f"{analysis_id}: contrato inesperado: {sorted(payload)}")

        if payload["returned_rows"] != len(payload["rows"]):
            raise RuntimeError(f"{analysis_id}: returned_rows incompatível.")

        if payload["returned_rows"] > payload["total_rows"]:
            raise RuntimeError(f"{analysis_id}: returned_rows maior que total_rows.")

    print("self_check_payload: ok")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="POC 3 - Execucao deterministica por analysis_catalog.yml")
    parser.add_argument(
        "--catalog",
        default="rfcs/agente-sped-fiscal/research/data/analysis_catalog.yml",
        help="Caminho do catalogo YAML.",
    )
    parser.add_argument(
        "--fixture",
        default="rfcs/agente-sped-fiscal/research/data/sped_icms_analysis_reference.txt",
        help="Fixture SPED sintetica.",
    )
    parser.add_argument(
        "--layout-source",
        default="rfcs/agente-sped-fiscal/research/data/sped_layout_fields.csv",
        help="CSV do leiaute.",
    )
    parser.add_argument(
        "--layout-catalog",
        default="layout_catalog.duckdb",
        help="Catalogo DuckDB de leiaute (temporario).",
    )
    parser.add_argument("--show-json", action="store_true", help="Exibe resultados completos em JSON.")
    parser.add_argument(
        "--self-checks",
        action="store_true",
        help="Executa checagens de bloqueio SQL reproduziveis.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    catalog_path = Path(args.catalog).resolve()
    fixture_path = Path(args.fixture).resolve()
    layout_source = Path(args.layout_source).resolve()
    layout_catalog = Path(args.layout_catalog).resolve()

    compile_layout_catalog(csv_path=layout_source, output_db=layout_catalog, layout_version="020")
    layout_metadata = load_layout_metadata(str(layout_catalog), sped_type="EFD-ICMS-IPI", layout_version="020")

    parsed = parse_sped(str(fixture_path), layout_metadata)
    file_profile = parsed["file_profile"]

    catalog_ctx = load_catalog(catalog_path)

    conn = duckdb.connect(database=":memory:")
    try:
        relation_info = materialize_relations(conn, parsed, layout_metadata)

        results: dict[str, dict[str, Any]] = {}
        for analysis_id, analysis in catalog_ctx.analyses.items():
            results[analysis_id] = execute_analysis(
                conn=conn,
                analysis_id=analysis_id,
                analysis=analysis,
                file_profile=file_profile,
                relation_info=relation_info,
            )

        expected = catalog_ctx.catalog.get("reference_validation", {}).get("expected", {})
        status_issues = [
            f"{analysis_id}: status inesperado {result.get('status')}"
            for analysis_id, result in results.items()
            if result.get("status") != "ok"
        ]
        reference_issues = validate_reference_expectations(results, expected)
        issues = status_issues + reference_issues

        print("=== POC 3 RESULT ===")
        print(f"catalog: {catalog_path}")
        print(f"fixture: {fixture_path}")
        print(f"analyses: {len(catalog_ctx.analyses)}")
        print(f"relations: {len(relation_info)}")
        print("\nStatus por analise:")
        for aid, res in results.items():
            print(f"- {aid}: status={res['status']} rows={res['row_count']}")

        if issues:
            print("\nDivergencias de referencia:")
            for item in issues:
                print(f"- {item}")
        else:
            print("\nReferência: sem divergências após validação normalizada.")

        if args.show_json:
            print("\nJSON (resultado completo):")
            print(json.dumps(results, ensure_ascii=False, indent=2, default=str))

        if args.self_checks:
            print("\nself_check_sql_guard:")
            for sql in ["DROP TABLE x", "SELECT 1; SELECT 2"]:
                assert_sql_blocked(sql)

            for analysis_id, analysis in catalog_ctx.analyses.items():
                result_cfg = analysis.get("result", {})
                narrative_input = analysis.get("narrative", {}).get("input", {})
                bounded_int(
                    result_cfg.get("row_limit", 5000),
                    name=f"{analysis_id}.result.row_limit",
                    minimum=1,
                    maximum=5000,
                )
                bounded_int(
                    narrative_input.get("max_rows", 20),
                    name=f"{analysis_id}.narrative.input.max_rows",
                    minimum=0,
                    maximum=100,
                )

            assert_masking_contract(results=results, analyses=catalog_ctx.analyses)
            assert_payload_contract(results=results)

        if issues:
            print("\nReferencia: divergencias apos validacao normalizada.")
            return 1

        # Exemplo de payload mascarado para evidencia de PR.
        sample = next(iter(results.values())) if results else None
        if sample:
            print("\nExemplo payload mascarado (primeira analise):")
            print(json.dumps(sample.get("masked_payload", {}), ensure_ascii=False, indent=2, default=str))

        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
