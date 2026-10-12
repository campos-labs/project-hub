from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, TypedDict

import duckdb
from langgraph.graph import END, START, StateGraph


SPED_SAMPLE = """|0000|020|0|01012026|31012026|EMPRESA SINTETICA|99999999000191||||||||A|0|
|C001|0|
|C100|1|0|CLI_A|55|00|1|1001|CHV1|02012026|02012026|30000,00|0|0|0|30000,00|9|0|0|0|30000,00|5400,00|0|0|0|0|0|0|0|
|C190|000|5102|18,00|30000,00|30000,00|5400,00|0|0|0|0||
|C100|1|0|CLI_A|55|00|1|1002|CHV2|03012026|03012026|40000,00|0|0|0|40000,00|9|0|0|0|40000,00|7200,00|0|0|0|0|0|0|0|
|C190|000|6102|18,00|40000,00|40000,00|7200,00|0|0|0|0||
|C100|1|0|CLI_A|55|02|1|2001|CHV3|04012026|04012026|900000,00|0|0|0|900000,00|9|0|0|0|900000,00|162000,00|0|0|0|0|0|0|0|
|C190|000|5102|18,00|900000,00|900000,00|162000,00|0|0|0|0||
|C990|7|
|9999|10|"""


class POCState(TypedDict):
    question: str
    analysis_id: str
    parameters: dict[str, Any]
    sql: str
    result_rows: list[dict[str, Any]]
    total_rows: int
    error: str
    narrative: str
    route: str
    use_gemini: bool
    narrative_mode: str
    provider_error: str


@dataclass
class AnalysisSpec:
    analysis_id: str
    sql: str


@dataclass(frozen=True)
class NarrativeAttempt:
    text: str | None
    error: str


ANALYSES = {
    "movement_by_cfop": AnalysisSpec(
        analysis_id="movement_by_cfop",
        sql=(
            """
            WITH grouped AS (
                SELECT c190.cfop, SUM(c190.vl_opr) AS total_operacao
                FROM c190
                JOIN c100 ON c190.parent_record_id = c100.record_id
                WHERE c100.ind_oper = '1'
                  AND c100.cod_sit IN ('00', '01')
                  AND c100.dt_doc BETWEEN ? AND ?
                GROUP BY c190.cfop
            ), ranked AS (
                SELECT
                    cfop,
                    total_operacao,
                    SUM(total_operacao) OVER () AS total_operacoes,
                    ROW_NUMBER() OVER (ORDER BY total_operacao DESC, cfop) AS rn
                FROM grouped
            )
            SELECT
                cfop,
                total_operacao,
                total_operacoes,
                ROUND((total_operacao / NULLIF(total_operacoes, 0)) * 100, 4) AS participation_percent
            FROM ranked
            WHERE rn <= ?
            ORDER BY rn
            """
        ),
    )
}


KEYWORDS = {
    "movement_by_cfop": ["cfop", "saidas", "saídas", "movimentacao", "movimentação"],
}


def br_to_date(value: str) -> date:
    return date(int(value[4:8]), int(value[2:4]), int(value[0:2]))


def br_to_decimal(value: str) -> Decimal:
    return Decimal(value.replace(".", "").replace(",", "."))


def parse_records(sped_text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    c100_rows: list[dict[str, Any]] = []
    c190_rows: list[dict[str, Any]] = []
    record_id = 0
    current_c100_id: int | None = None

    for line in sped_text.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 3:
            continue
        code = parts[1]
        if not code:
            continue

        record_id += 1

        if code == "C100":
            current_c100_id = record_id
            c100_rows.append(
                {
                    "record_id": record_id,
                    "ind_oper": parts[2],
                    "cod_sit": parts[6],
                    "dt_doc": br_to_date(parts[10]),
                }
            )
        elif code == "C190" and current_c100_id is not None:
            c190_rows.append(
                {
                    "record_id": record_id,
                    "parent_record_id": current_c100_id,
                    "cfop": parts[3],
                    "vl_opr": br_to_decimal(parts[5]),
                }
            )

    return c100_rows, c190_rows


def create_duckdb(c100_rows: list[dict[str, Any]], c190_rows: list[dict[str, Any]]) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(database=":memory:")
    conn.execute(
        "CREATE TABLE c100(record_id BIGINT, ind_oper VARCHAR, cod_sit VARCHAR, dt_doc DATE)"
    )
    conn.execute(
        "CREATE TABLE c190(record_id BIGINT, parent_record_id BIGINT, cfop VARCHAR, vl_opr DECIMAL(18,2))"
    )
    if c100_rows:
        conn.executemany(
            "INSERT INTO c100 VALUES (?, ?, ?, ?)",
            [(r["record_id"], r["ind_oper"], r["cod_sit"], r["dt_doc"]) for r in c100_rows],
        )
    if c190_rows:
        conn.executemany(
            "INSERT INTO c190 VALUES (?, ?, ?, ?)",
            [(r["record_id"], r["parent_record_id"], r["cfop"], r["vl_opr"]) for r in c190_rows],
        )
    return conn


def route_question_node(state: POCState) -> dict[str, Any]:
    q = state["question"].lower()
    for analysis_id, words in KEYWORDS.items():
        if any(word in q for word in words):
            route = "known"
            if "fevereiro" in q:
                start_date = date(2026, 2, 1)
                end_date = date(2026, 2, 28)
            else:
                start_date = date(2026, 1, 1)
                end_date = date(2026, 1, 31)

            return {
                "analysis_id": analysis_id,
                "parameters": {"start_date": start_date, "end_date": end_date, "top_n": 3},
                "sql": ANALYSES[analysis_id].sql,
                "route": route,
            }

    return {
        "analysis_id": "",
        "parameters": {},
        "sql": "",
        "route": "unknown",
        "error": "Pergunta fora do catálogo da POC.",
    }


def execute_analysis_node(state: POCState) -> dict[str, Any]:
    if state.get("analysis_id") != "movement_by_cfop":
        return {"result_rows": [], "total_rows": 0}

    c100_rows, c190_rows = parse_records(SPED_SAMPLE)
    conn = create_duckdb(c100_rows, c190_rows)

    try:
        params = state["parameters"]
        rows = conn.execute(
            state["sql"],
            [params["start_date"], params["end_date"], params["top_n"]],
        ).fetchall()
        columns = [desc[0] for desc in conn.description]
        result_rows = [dict(zip(columns, row)) for row in rows]
        return {"result_rows": result_rows, "total_rows": len(result_rows), "error": ""}
    except Exception as exc:
        return {"result_rows": [], "total_rows": 0, "error": f"Falha de execução: {exc}"}
    finally:
        conn.close()


def format_offline_narrative(state: POCState) -> str:
    if state.get("route") == "unknown":
        return "Não reconheci uma análise suportada nesta POC. Tente perguntar sobre saídas por CFOP."
    if state.get("error"):
        return f"Execução não concluída com segurança: {state['error']}"
    if not state.get("result_rows"):
        return "A consulta executou com sucesso, mas não encontrou linhas para o período informado."

    rows = state["result_rows"]
    total_operacoes = rows[0]["total_operacoes"]
    top = "; ".join(
        f"CFOP {r['cfop']} = {r['total_operacao']}" for r in rows
    )
    return f"Total de saídas consideradas: {total_operacoes}. Top CFOPs: {top}."


def try_gemini_narrative(state: POCState) -> NarrativeAttempt:
    if not state.get("use_gemini", False):
        return NarrativeAttempt(text=None, error="")

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return NarrativeAttempt(text=None, error="GOOGLE_API_KEY ausente")

    try:
        from langchain.chat_models import init_chat_model

        llm = init_chat_model(
            model="gemini-2.5-flash",
            model_provider="google_genai",
            temperature=0,
            api_key=api_key,
        )
        prompt = (
            "Voce e um sintetizador tecnico. Nao recalcule valores; use apenas o resultado JSON. "
            "Se vazio, informe explicitamente. Responda em ate 3 frases.\n"
            f"analysis_id={state.get('analysis_id')}\n"
            f"result={json.dumps(state.get('result_rows', []), default=str, ensure_ascii=False)}"
        )
        answer = llm.invoke(prompt)
        return NarrativeAttempt(text=str(answer.content), error="")
    except ImportError:
        return NarrativeAttempt(text=None, error="dependencia Gemini ausente")
    except Exception:
        return NarrativeAttempt(text=None, error="falha do provedor Gemini")


def narrative_node(state: POCState) -> dict[str, Any]:
    # Pergunta fora do catalogo, erro de execucao e resultado vazio sao respostas deterministicas.
    if state.get("route") == "unknown" or state.get("error") or not state.get("result_rows"):
        return {"narrative": format_offline_narrative(state), "narrative_mode": "offline", "provider_error": ""}

    gemini_attempt = try_gemini_narrative(state)
    if gemini_attempt.text:
        return {"narrative": gemini_attempt.text, "narrative_mode": "gemini", "provider_error": ""}
    return {
        "narrative": format_offline_narrative(state),
        "narrative_mode": "offline_fallback" if state.get("use_gemini", False) else "offline",
        "provider_error": gemini_attempt.error if state.get("use_gemini", False) else "",
    }


def route_edge(state: POCState) -> str:
    if state.get("route") == "unknown":
        return "NARRATIVE"
    return "EXECUTE"


def build_graph():
    workflow = StateGraph(POCState)
    workflow.add_node("ROUTE", route_question_node)
    workflow.add_node("EXECUTE", execute_analysis_node)
    workflow.add_node("NARRATIVE", narrative_node)

    workflow.add_edge(START, "ROUTE")
    workflow.add_conditional_edges("ROUTE", route_edge, {"EXECUTE": "EXECUTE", "NARRATIVE": "NARRATIVE"})
    workflow.add_edge("EXECUTE", "NARRATIVE")
    workflow.add_edge("NARRATIVE", END)
    return workflow.compile()


def run_once(question: str, use_gemini: bool = False) -> POCState:
    graph = build_graph()
    initial_state: POCState = {
        "question": question,
        "analysis_id": "",
        "parameters": {},
        "sql": "",
        "result_rows": [],
        "total_rows": 0,
        "error": "",
        "narrative": "",
        "route": "",
        "use_gemini": use_gemini,
        "narrative_mode": "",
        "provider_error": "",
    }
    return graph.invoke(initial_state)


def print_result(result: POCState) -> None:
    print("\n=== EXECUCAO POC 1 ===")
    print(f"route: {result.get('route')}")
    print(f"analysis_id: {result.get('analysis_id') or '<none>'}")
    print(f"parameters: {result.get('parameters')}")
    if result.get("sql"):
        print("sql: movement_by_cfop (query controlada)")
    if result.get("error"):
        print(f"error: {result['error']}")
    print(f"narrative_mode: {result.get('narrative_mode') or '<none>'}")
    if result.get("provider_error"):
        print(f"provider_error: {result['provider_error']}")
    print(f"rows: {result.get('total_rows', 0)}")
    print("result_json:")
    print(json.dumps(result.get("result_rows", []), ensure_ascii=False, indent=2, default=str))
    print("narrative:")
    print(result.get("narrative", ""))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="POC 1 - Fluxo LangGraph + DuckDB controlado.")
    parser.add_argument(
        "--question",
        default="Qual foi o valor das saídas por CFOP em janeiro?",
        help="Pergunta de entrada.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Executa cenarios minimos: reconhecida, desconhecida, periodo vazio.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--offline",
        action="store_true",
        help="Forca narrativa 100% offline (padrao).",
    )
    mode.add_argument(
        "--use-gemini",
        action="store_true",
        help="Permite tentativa de narrativa via Gemini (fallback offline em falha).",
    )
    parser.add_argument(
        "--self-checks",
        action="store_true",
        help="Executa validacoes reproduziveis da demo e do modo offline.",
    )
    return parser.parse_args()


def run_self_checks() -> int:
    def check(condition: bool, message: str) -> None:
        if not condition:
            raise RuntimeError(message)

    checks = [
        (
            "known",
            run_once("Qual foi o valor das saídas por CFOP em janeiro?", use_gemini=False),
            {"route": "known", "analysis_id": "movement_by_cfop", "total_rows": 2, "narrative_mode": "offline"},
        ),
        (
            "empty-period",
            run_once("Mostre saidas por cfop em fevereiro", use_gemini=False),
            {"route": "known", "analysis_id": "movement_by_cfop", "total_rows": 0, "narrative_mode": "offline"},
        ),
        (
            "unknown",
            run_once("Me conte uma piada", use_gemini=False),
            {"route": "unknown", "analysis_id": "", "total_rows": 0, "narrative_mode": "offline"},
        ),
    ]

    for name, result, expected in checks:
        check(result.get("route") == expected["route"], f"{name}: route esperado {expected['route']} obtido {result.get('route')}")
        check(result.get("analysis_id") == expected["analysis_id"], f"{name}: analysis_id esperado {expected['analysis_id']} obtido {result.get('analysis_id')}")
        check(result.get("total_rows", 0) == expected["total_rows"], f"{name}: total_rows esperado {expected['total_rows']} obtido {result.get('total_rows', 0)}")
        check(result.get("narrative_mode") == expected["narrative_mode"], f"{name}: narrative_mode esperado {expected['narrative_mode']} obtido {result.get('narrative_mode')}")
        check(not result.get("provider_error"), f"{name}: provider_error inesperado {result.get('provider_error')}")

    original_key = os.environ.pop("GOOGLE_API_KEY", None)
    try:
        fallback_result = run_once("Qual foi o valor das saídas por CFOP em janeiro?", use_gemini=True)
        check(fallback_result.get("narrative_mode") == "offline_fallback", f"fallback: narrative_mode obtido {fallback_result.get('narrative_mode')}")
        check(fallback_result.get("provider_error") == "GOOGLE_API_KEY ausente", f"fallback: provider_error obtido {fallback_result.get('provider_error')}")
    finally:
        if original_key is not None:
            os.environ["GOOGLE_API_KEY"] = original_key

    print("self_checks: ok")
    return 0


def main() -> int:
    args = parse_args()
    if args.self_checks:
        return run_self_checks()
    use_gemini = bool(args.use_gemini and not args.offline)
    if args.demo:
        scenarios = [
            "Qual foi o valor das saídas por CFOP em janeiro?",
            "Mostre saidas por cfop em fevereiro",
            "Me conte uma piada",
        ]
        for question in scenarios:
            print(f"\n>>> question: {question}")
            print_result(run_once(question, use_gemini=use_gemini))
        return 0

    print_result(run_once(args.question, use_gemini=use_gemini))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
