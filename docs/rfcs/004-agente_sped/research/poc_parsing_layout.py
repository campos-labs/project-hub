from __future__ import annotations

import argparse
import json
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from build_layout_catalog import compile_layout_catalog


@dataclass(frozen=True)
class FieldMeta:
    field_position: int
    field_code: str
    field_type: str
    field_required: bool


@dataclass(frozen=True)
class RecordMeta:
    record_code: str
    record_level: int | None
    fields: list[FieldMeta]


def parse_br_date(value: str) -> date | None:
    value = value.strip()
    if len(value) != 8 or not value.isdigit():
        return None
    return date(int(value[4:8]), int(value[2:4]), int(value[0:2]))


def parse_br_decimal(value: str) -> Decimal | None:
    value = value.strip()
    if not value:
        return None
    try:
        return Decimal(value.replace(".", "").replace(",", "."))
    except Exception:
        return None


def coerce_value(raw: str, field_type: str) -> Any:
    field_type = field_type.strip().lower()
    if field_type in {"float", "double", "decimal"}:
        return parse_br_decimal(raw)
    if field_type == "date":
        return parse_br_date(raw)
    if field_type in {"int", "integer", "bigint"}:
        try:
            return int(raw.strip()) if raw.strip() else None
        except ValueError:
            return None
    return raw


def load_layout_metadata(layout_catalog_path: str, sped_type: str = "EFD-ICMS-IPI", layout_version: str = "020") -> dict[str, RecordMeta]:
    conn = duckdb.connect(layout_catalog_path, read_only=True)
    try:
        rows = conn.execute(
            """
            SELECT record_code, record_level, field_position, field_code, field_type, field_required
            FROM layout_fields
            WHERE sped_type = ? AND layout_version = ?
            ORDER BY record_code, field_position
            """,
            [sped_type, layout_version],
        ).fetchall()
    finally:
        conn.close()

    grouped: dict[str, list[FieldMeta]] = {}
    levels: dict[str, int | None] = {}
    for record_code, record_level, field_pos, field_code, field_type, field_required in rows:
        grouped.setdefault(record_code, []).append(
            FieldMeta(
                field_position=int(field_pos),
                field_code=str(field_code),
                field_type=str(field_type),
                field_required=bool(field_required),
            )
        )
        levels[record_code] = int(record_level) if record_level is not None else None

    metadata: dict[str, RecordMeta] = {}
    for record_code, fields in grouped.items():
        metadata[record_code] = RecordMeta(
            record_code=record_code,
            record_level=levels.get(record_code),
            fields=fields,
        )

    if not metadata:
        raise ValueError("Catalogo vazio para sped_type/layout_version informado.")

    return metadata


def detect_layout_version(sped_lines: list[str]) -> str:
    for line in sped_lines:
        parts = line.strip().split("|")
        if len(parts) > 2 and parts[1] == "0000":
            return parts[2].strip()
    raise ValueError("Registro 0000 nao encontrado para detectar COD_VER.")


def detect_layout_version_from_file(sped_path: Path) -> str:
    with sped_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            parts = line.strip().split("|")
            if len(parts) > 2 and parts[1] == "0000":
                return parts[2].strip()
    raise ValueError("Registro 0000 nao encontrado para detectar COD_VER.")


def build_record_index(records: list[dict[str, Any]]) -> dict[str, list[int]]:
    index: dict[str, list[int]] = {}
    for rec in records:
        index.setdefault(rec["record_code"], []).append(rec["record_id"])
    return index


def validate_hierarchy(records: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    last_level = 0
    for rec in records:
        level = rec.get("record_level")
        if level is None:
            continue
        if level > last_level + 1:
            errors.append(
                f"Salto de nivel na linha {rec['source_line']}: {last_level} -> {level} (registro {rec['record_code']})."
            )
        last_level = level
    return errors


def parse_sped(sped_path: str, layout_metadata: dict[str, RecordMeta]) -> dict[str, Any]:
    file_id = str(uuid.uuid4())
    records: list[dict[str, Any]] = []
    unknown_codes: list[str] = []
    invalid_lines: list[dict[str, Any]] = []
    level_stack: list[tuple[int, int, str]] = []
    record_counter = 0
    field_error_count = 0
    invalid_line_count = 0
    version: str | None = None
    total_lines = 0

    with Path(sped_path).open("r", encoding="utf-8") as handle:
        for i, line in enumerate(handle, start=1):
            total_lines = i
            line = line.rstrip("\n\r")
            parts = line.split("|")
            if len(parts) < 3:
                invalid_line_count += 1
                invalid_lines.append({"source_line": i, "reason": "estrutura insuficiente"})
                continue

            record_code = parts[1].strip()
            if not record_code:
                invalid_line_count += 1
                invalid_lines.append({"source_line": i, "reason": "codigo de registro vazio"})
                continue

            if record_code == "0000" and version is None:
                version = parts[2].strip()
                if version != "020":
                    raise ValueError(f"COD_VER nao suportado na POC: {version}. Esperado: 020")

            record_counter += 1
            meta = layout_metadata.get(record_code)
            record_level = meta.record_level if meta else None

            parent_record_id = None
            parent_record_code = None

            if record_level is not None:
                while level_stack and level_stack[-1][0] >= record_level:
                    level_stack.pop()
                if level_stack:
                    parent_record_id = level_stack[-1][1]
                    parent_record_code = level_stack[-1][2]
                level_stack.append((record_level, record_counter, record_code))

            raw_values: dict[str, Any] = {}
            typed_values: dict[str, Any] = {}
            field_errors: list[str] = []

            if meta:
                for f in meta.fields:
                    raw = parts[f.field_position].strip() if len(parts) > f.field_position else ""
                    raw_values[f.field_code] = raw
                    typed_values[f.field_code] = coerce_value(raw, f.field_type)
                    if f.field_required and not raw:
                        field_errors.append(f"Campo obrigatorio ausente: {f.field_code}")
                field_error_count += len(field_errors)
            else:
                unknown_codes.append(record_code)

            records.append(
                {
                    "file_id": file_id,
                    "record_id": record_counter,
                    "record_code": record_code,
                    "record_level": record_level,
                    "source_line": i,
                    "parent_record_id": parent_record_id,
                    "parent_record_code": parent_record_code,
                    "values": raw_values,
                    "raw_values": raw_values,
                    "typed_values": typed_values,
                    "field_errors": field_errors,
                    "unknown_record": meta is None,
                }
            )

    if version is None:
        raise ValueError("Registro 0000 nao encontrado para detectar COD_VER.")

    index = build_record_index(records)
    hierarchy_errors = validate_hierarchy(records)

    period_start = None
    period_end = None
    for rec in records:
        if rec["record_code"] == "0000":
            period_start = parse_br_date(rec["values"].get("DT_INI", "") or rec["values"].get("field_4", ""))
            period_end = parse_br_date(rec["values"].get("DT_FIN", "") or rec["values"].get("field_5", ""))
            break

    counts = Counter(r["record_code"] for r in records)
    file_profile = {
        "file_id": file_id,
        "sped_type": "EFD-ICMS-IPI",
        "layout_version": version,
        "period_start": str(period_start) if period_start else None,
        "period_end": str(period_end) if period_end else None,
        "total_lines": total_lines,
        "parsed_records": len(records),
        "invalid_line_count": invalid_line_count,
        "field_error_count": field_error_count,
        "counts_by_record_code": dict(sorted(counts.items())),
        "unknown_records": sorted(set(unknown_codes)),
        "hierarchy_errors": hierarchy_errors,
        "invalid_lines": invalid_lines[:20],
    }

    return {
        "file_profile": file_profile,
        "records": records,
        "record_index": index,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="POC 2 - Parsing orientado ao leiaute com rastreabilidade.")
    parser.add_argument(
        "--sped",
        default="rfcs/agente-sped-fiscal/research/data/sped_icms_analysis_reference.txt",
        help="Caminho do arquivo SPED.",
    )
    parser.add_argument(
        "--layout-source",
        default="rfcs/agente-sped-fiscal/research/data/sped_layout_fields.csv",
        help="CSV de leiaute.",
    )
    parser.add_argument(
        "--layout-catalog",
        default="layout_catalog.duckdb",
        help="Arquivo DuckDB do catalogo de leiaute.",
    )
    parser.add_argument(
        "--print-sample",
        action="store_true",
        help="Mostra amostra de rastreabilidade C001 -> C100 -> C170/C190 e K100 -> K230.",
    )
    parser.add_argument(
        "--self-checks",
        action="store_true",
        help="Executa checagem reproduzivel de rejeicao de COD_VER nao suportado.",
    )
    return parser.parse_args()


def print_trace_examples(records: list[dict[str, Any]]) -> None:
    print("\n=== EXEMPLOS DE RASTREABILIDADE ===")
    interesting = {"C001", "C100", "C170", "C190", "K100", "K230"}
    for rec in records:
        if rec["record_code"] in interesting:
            print(
                f"line={rec['source_line']} record_id={rec['record_id']} code={rec['record_code']} "
                f"parent_id={rec['parent_record_id']} parent_code={rec['parent_record_code']}"
            )


def main() -> int:
    args = parse_args()
    sped_path = Path(args.sped).resolve()
    layout_source = Path(args.layout_source).resolve()
    layout_catalog = Path(args.layout_catalog).resolve()

    detected_version = detect_layout_version_from_file(sped_path)
    if detected_version != "020":
        raise ValueError(f"COD_VER nao suportado na POC: {detected_version}. Esperado: 020")

    compile_layout_catalog(csv_path=layout_source, output_db=layout_catalog, layout_version=detected_version)
    metadata = load_layout_metadata(str(layout_catalog), sped_type="EFD-ICMS-IPI", layout_version=detected_version)
    parsed = parse_sped(str(sped_path), metadata)

    print("=== POC 2 PARSING RESULT ===")
    print(f"sped: {sped_path}")
    print(f"layout_catalog: {layout_catalog}")
    print(json.dumps(parsed["file_profile"], ensure_ascii=False, indent=2))

    if args.print_sample:
        print_trace_examples(parsed["records"])

    if args.self_checks:
        tmp_path = sped_path.with_name("tmp_invalid_codver_selfcheck.txt")
        tmp_path.write_text("|0000|019|\n|9999|0|\n", encoding="utf-8")
        try:
            try:
                parse_sped(str(tmp_path), metadata)
                raise RuntimeError("self_check_codver: rejeicao esperada nao ocorreu")
            except Exception as exc:
                msg = str(exc)
                if "COD_VER nao suportado" not in msg:
                    raise RuntimeError(f"self_check_codver: mensagem inesperada: {msg}") from exc
                print(f"self_check_codver: ok ({msg})")
        finally:
            tmp_path.unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
