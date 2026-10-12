from __future__ import annotations

import argparse
import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path

import duckdb


@dataclass(frozen=True)
class LayoutField:
    sped_type: str
    layout_version: str
    record_code: str
    record_level: int | None
    record_description: str
    field_position: int
    field_code: str
    field_type: str
    field_required: bool
    field_description: str


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"true", "1", "yes", "sim", "o"}


def parse_int(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def make_unique_fieldnames(fieldnames: list[str]) -> list[str]:
    counts: dict[str, int] = {}
    unique_fieldnames: list[str] = []

    for fieldname in fieldnames:
        normalized = fieldname.strip()
        counts[normalized] = counts.get(normalized, 0) + 1
        if counts[normalized] == 1:
            unique_fieldnames.append(normalized)
        else:
            unique_fieldnames.append(f"{normalized}__{counts[normalized]}")

    return unique_fieldnames


def get_column_name(fieldnames: list[str], base_name: str, occurrence: int = 1) -> str:
    seen = 0
    for fieldname in fieldnames:
        normalized = fieldname.split("__", 1)[0]
        if normalized == base_name:
            seen += 1
            if seen == occurrence:
                return fieldname
    raise ValueError(f"Coluna obrigatoria ausente no CSV: {base_name} ({occurrence}ª ocorrencia)")


def normalize_rows(csv_path: Path, layout_version: str) -> list[LayoutField]:
    rows: list[LayoutField] = []
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        first_line = f.readline()
        if not first_line:
            raise ValueError("CSV de leiaute invalido: arquivo vazio.")

        header_line = f.readline()
        if not header_line:
            raise ValueError("CSV de leiaute invalido: cabecalho ausente.")

        raw_header = next(csv.reader([header_line]))
        fieldnames = make_unique_fieldnames(raw_header)
        reader = csv.DictReader(f, fieldnames=fieldnames)

        col_type_sped = get_column_name(fieldnames, "type_sped")
        col_record_level = get_column_name(fieldnames, "level")
        col_record_desc = get_column_name(fieldnames, "desc")
        col_record_code = get_column_name(fieldnames, "register")
        col_field_position = get_column_name(fieldnames, "index")
        col_field_code = get_column_name(fieldnames, "code", occurrence=2)
        col_field_type = get_column_name(fieldnames, "type")
        col_field_required = get_column_name(fieldnames, "required", occurrence=2)
        col_field_desc = get_column_name(fieldnames, "desc", occurrence=2)

        for raw in reader:
            if not raw:
                continue

            sped_type = (raw.get(col_type_sped) or "").strip()
            record_code = (raw.get(col_record_code) or "").strip()
            field_code = (raw.get(col_field_code) or "").strip()

            if not sped_type or not record_code or not field_code:
                continue

            record_level = parse_int(raw.get(col_record_level, ""))
            record_desc = (raw.get(col_record_desc) or "").strip()
            field_pos = parse_int(raw.get(col_field_position, ""))
            if field_pos is None:
                continue

            field_type = (raw.get(col_field_type) or "").strip().lower() or "char"
            field_required = parse_bool(raw.get(col_field_required, ""))
            field_desc = (raw.get(col_field_desc) or "").strip()

            rows.append(
                LayoutField(
                    sped_type=sped_type,
                    layout_version=layout_version,
                    record_code=record_code,
                    record_level=record_level,
                    record_description=record_desc,
                    field_position=field_pos,
                    field_code=field_code,
                    field_type=field_type,
                    field_required=field_required,
                    field_description=field_desc,
                )
            )

        if not rows:
            raise ValueError("Nenhuma linha de metadado valida foi carregada do CSV.")

        return rows


def compile_layout_catalog(csv_path: Path, output_db: Path, layout_version: str = "020") -> dict[str, int]:
    fields = normalize_rows(csv_path=csv_path, layout_version=layout_version)

    output_db.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(output_db))

    try:
        conn.execute("DROP TABLE IF EXISTS layout_fields")
        conn.execute("DROP TABLE IF EXISTS layout_records")
        conn.execute("DROP TABLE IF EXISTS layout_source")

        conn.execute(
            """
            CREATE TABLE layout_fields (
                sped_type VARCHAR,
                layout_version VARCHAR,
                record_code VARCHAR,
                record_level INTEGER,
                record_description VARCHAR,
                field_position INTEGER,
                field_code VARCHAR,
                field_type VARCHAR,
                field_required BOOLEAN,
                field_description VARCHAR
            )
            """
        )

        conn.executemany(
            """
            INSERT INTO layout_fields VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    f.sped_type,
                    f.layout_version,
                    f.record_code,
                    f.record_level,
                    f.record_description,
                    f.field_position,
                    f.field_code,
                    f.field_type,
                    f.field_required,
                    f.field_description,
                )
                for f in fields
            ],
        )

        conn.execute(
            """
            CREATE TABLE layout_records AS
            SELECT
                sped_type,
                layout_version,
                record_code,
                MIN(record_level) AS record_level,
                ANY_VALUE(record_description) AS record_description,
                COUNT(*) AS field_count
            FROM layout_fields
            GROUP BY sped_type, layout_version, record_code
            """
        )

        csv_hash = hashlib.sha256(csv_path.read_bytes()).hexdigest()
        conn.execute(
            """
            CREATE TABLE layout_source (
                source_path VARCHAR,
                source_sha256 VARCHAR,
                layout_version VARCHAR
            )
            """
        )
        conn.execute(
            "INSERT INTO layout_source VALUES (?, ?, ?)",
            [str(csv_path), csv_hash, layout_version],
        )

        record_count = conn.execute("SELECT COUNT(*) FROM layout_records").fetchone()[0]
        field_count = conn.execute("SELECT COUNT(*) FROM layout_fields").fetchone()[0]

        return {"records": int(record_count), "fields": int(field_count)}
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compila CSV de leiaute em catalogo DuckDB temporario.")
    parser.add_argument(
        "--source",
        default="rfcs/agente-sped-fiscal/research/data/sped_layout_fields.csv",
        help="Caminho do CSV de leiaute.",
    )
    parser.add_argument(
        "--output",
        default="layout_catalog.duckdb",
        help="Caminho do DuckDB de saida.",
    )
    parser.add_argument("--layout-version", default="020", help="Versao de leiaute suportada na POC.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = Path(args.source).resolve()
    output = Path(args.output).resolve()

    stats = compile_layout_catalog(csv_path=source, output_db=output, layout_version=args.layout_version)

    print("=== BUILD LAYOUT CATALOG ===")
    print(f"source: {source}")
    print(f"output: {output}")
    print(f"layout_version: {args.layout_version}")
    print(f"records: {stats['records']}")
    print(f"fields: {stats['fields']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
