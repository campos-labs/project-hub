from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import yaml
from pydantic import BaseModel, Field


@dataclass
class RoutingDecision:
    analysis_id: str | None
    parameters: dict[str, Any]
    needs_clarification: bool
    out_of_catalog: bool
    strategy: str
    latency_ms: float
    provider_failure: bool = False
    error: str = ""


class RoutingDecisionSchema(BaseModel):
    analysis_id: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    needs_clarification: bool = False
    out_of_catalog: bool = False


def load_routing_projection(catalog_path: str) -> dict[str, dict[str, Any]]:
    with open(catalog_path, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)

    if not isinstance(raw, dict):
        raise ValueError("Catálogo YAML inválido.")

    analyses = raw.get("analyses")
    if not isinstance(analyses, dict) or not analyses:
        raise ValueError("Catálogo YAML sem análises de roteamento.")

    projection: dict[str, dict[str, Any]] = {}
    for aid, cfg in analyses.items():
        projection[aid] = {
            "title": cfg.get("title", ""),
            "utterances": cfg.get("routing", {}).get("utterances", []),
            "parameters": cfg.get("parameters", {}),
        }
    return projection


def tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-zA-Z0-9_çãõáéíóú]+", text.lower()))


def extract_parameters(question: str) -> dict[str, Any]:
    params: dict[str, Any] = {}
    q = question.lower()

    top_match = re.search(r"top\s*(\d+)", q)
    if top_match:
        params["top_n"] = int(top_match.group(1))

    pct_match = re.search(r"(\d{1,2}(?:[\.,]\d+)?)\s*%", q)
    if pct_match:
        params["threshold_percent"] = float(pct_match.group(1).replace(",", "."))

    if "janeiro" in q:
        params["start_date"] = "2026-01-01"
        params["end_date"] = "2026-01-31"
    elif "fevereiro" in q:
        params["start_date"] = "2026-02-01"
        params["end_date"] = "2026-02-28"

    return params


def deterministic_route(question: str, projection: dict[str, dict[str, Any]]) -> RoutingDecision:
    t0 = time.perf_counter()
    q_tokens = tokenize(question)

    best_id = None
    best_score = 0.0
    second_score = 0.0

    for aid, cfg in projection.items():
        utterances = cfg["utterances"] or [cfg["title"]]
        score = 0.0
        for utt in utterances:
            utt_tokens = tokenize(utt)
            if not utt_tokens:
                continue
            overlap = len(q_tokens.intersection(utt_tokens))
            score = max(score, overlap / max(1, len(utt_tokens)))

        if score > best_score:
            second_score = best_score
            best_score = score
            best_id = aid
        elif score > second_score:
            second_score = score

    latency = (time.perf_counter() - t0) * 1000

    if best_id is None or best_score < 0.15:
        return RoutingDecision(
            analysis_id=None,
            parameters={},
            needs_clarification=False,
            out_of_catalog=True,
            strategy="deterministic",
            latency_ms=latency,
        )

    ambiguous = abs(best_score - second_score) < 0.08 and second_score > 0
    return RoutingDecision(
        analysis_id=best_id,
        parameters=extract_parameters(question),
        needs_clarification=ambiguous,
        out_of_catalog=False,
        strategy="deterministic",
        latency_ms=latency,
    )


def route_with_gemini(question: str, projection: dict[str, dict[str, Any]]) -> RoutingDecision:
    t0 = time.perf_counter()
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return RoutingDecision(
            analysis_id=None,
            parameters={},
            needs_clarification=False,
            out_of_catalog=False,
            strategy="gemini_structured",
            latency_ms=(time.perf_counter() - t0) * 1000,
            provider_failure=True,
            error="GOOGLE_API_KEY ausente",
        )

    try:
        from langchain.chat_models import init_chat_model

        llm = init_chat_model(
            model="gemini-2.5-flash",
            model_provider="google_genai",
            temperature=0,
            api_key=api_key,
        )

        candidates = [
            {
                "analysis_id": aid,
                "title": cfg["title"],
                "utterances": cfg["utterances"][:4],
                "parameters": list(cfg["parameters"].keys()),
            }
            for aid, cfg in projection.items()
        ]

        structured_llm = llm.with_structured_output(RoutingDecisionSchema)
        prompt = (
            "Classifique a pergunta em UM analysis_id candidato ou out_of_catalog=true. "
            "Use a saida estruturada e nao gere SQL.\n"
            f"Pergunta: {question}\n"
            f"Candidatos: {json.dumps(candidates, ensure_ascii=False)}"
        )
        parsed = structured_llm.invoke(prompt)
        if isinstance(parsed, RoutingDecisionSchema):
            parsed = parsed.model_dump()
        elif hasattr(parsed, "model_dump"):
            parsed = parsed.model_dump()
        elif not isinstance(parsed, dict):
            parsed = dict(parsed)

        latency = (time.perf_counter() - t0) * 1000
        analysis_id = parsed.get("analysis_id") or None
        if analysis_id is not None and analysis_id not in projection:
            return RoutingDecision(
                analysis_id=None,
                parameters={},
                needs_clarification=False,
                out_of_catalog=False,
                strategy="gemini_structured",
                latency_ms=latency,
                provider_failure=True,
                error=f"analysis_id inexistente: {analysis_id}",
            )

        if bool(parsed.get("out_of_catalog")) and analysis_id is not None:
            return RoutingDecision(
                analysis_id=None,
                parameters={},
                needs_clarification=False,
                out_of_catalog=False,
                strategy="gemini_structured",
                latency_ms=latency,
                provider_failure=True,
                error="contrato inconsistente do provider",
            )

        parameters = parsed.get("parameters") or {}
        if not isinstance(parameters, dict):
            return RoutingDecision(
                analysis_id=None,
                parameters={},
                needs_clarification=False,
                out_of_catalog=False,
                strategy="gemini_structured",
                latency_ms=latency,
                provider_failure=True,
                error="parameters nao e objeto",
            )

        if analysis_id is not None:
            allowed_parameters = set(projection[analysis_id].get("parameters", {}).keys())
            unknown_parameters = sorted(set(parameters.keys()) - allowed_parameters)
            if unknown_parameters:
                return RoutingDecision(
                    analysis_id=None,
                    parameters={},
                    needs_clarification=False,
                    out_of_catalog=False,
                    strategy="gemini_structured",
                    latency_ms=latency,
                    provider_failure=True,
                    error="parametros nao declarados: " + ", ".join(unknown_parameters),
                )
        elif parameters:
            return RoutingDecision(
                analysis_id=None,
                parameters={},
                needs_clarification=False,
                out_of_catalog=False,
                strategy="gemini_structured",
                latency_ms=latency,
                provider_failure=True,
                error="parametros retornados sem analysis_id",
            )

        needs_clarification = bool(parsed.get("needs_clarification", False))
        out_of_catalog = bool(parsed.get("out_of_catalog", False))

        if analysis_id is None and not out_of_catalog and not needs_clarification:
            return RoutingDecision(
                analysis_id=None,
                parameters={},
                needs_clarification=False,
                out_of_catalog=False,
                strategy="gemini_structured",
                latency_ms=latency,
                provider_failure=True,
                error="contrato sem decisao de roteamento",
            )

        return RoutingDecision(
            analysis_id=analysis_id,
            parameters=parameters,
            needs_clarification=needs_clarification,
            out_of_catalog=out_of_catalog,
            strategy="gemini_structured",
            latency_ms=latency,
        )
    except Exception as exc:
        return RoutingDecision(
            analysis_id=None,
            parameters={},
            needs_clarification=False,
            out_of_catalog=False,
            strategy="gemini_structured",
            latency_ms=(time.perf_counter() - t0) * 1000,
            provider_failure=True,
            error=f"falha provider: {type(exc).__name__}",
        )


def prefilter_then_gemini(question: str, projection: dict[str, dict[str, Any]]) -> RoutingDecision:
    base = deterministic_route(question, projection)
    if base.out_of_catalog:
        decision = route_with_gemini(question, projection)
        decision.strategy = "prefilter_plus_gemini"
        return decision

    shortlist = {}
    q_tokens = tokenize(question)
    for aid, cfg in projection.items():
        score = 0
        for utt in cfg["utterances"][:3]:
            score += len(q_tokens.intersection(tokenize(utt)))
        if score > 0:
            shortlist[aid] = cfg

    if not shortlist:
        shortlist = projection

    decision = route_with_gemini(question, shortlist)
    decision.strategy = "prefilter_plus_gemini"
    return decision


def deterministic_fallback_gemini(question: str, projection: dict[str, dict[str, Any]]) -> RoutingDecision:
    det = deterministic_route(question, projection)
    if det.out_of_catalog or det.needs_clarification:
        gm = route_with_gemini(question, projection)
        gm.strategy = "deterministic_fallback_gemini"
        if gm.error:
            det.provider_failure = True
            det.error = gm.error
            det.strategy = "deterministic_fallback_gemini"
            return det
        return gm
    det.strategy = "deterministic_fallback_gemini"
    return det


def evaluate_cases(strategy: str, projection: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    # Casos embutidos no script, como pedido pela issue.
    cases = [
        {"q": "Qual foi o valor das saídas por CFOP em janeiro?", "expected": "movement_by_cfop"},
        {"q": "Quais foram os CFOPs de saída mais relevantes?", "expected": "movement_by_cfop"},
        {"q": "Liste notas fiscais canceladas no periodo", "expected": "cancelled_documents"},
        {"q": "Mostre documentos denegados ou inutilizados", "expected": "cancelled_documents"},
        {"q": "Tem movimentacao no bloco K?", "expected": "block_k_activity"},
        {"q": "Há registros K230 no arquivo?", "expected": "block_k_activity"},
        {"q": "Algum fornecedor passou de 50% das compras?", "expected": "supplier_concentration"},
        {"q": "fornecedor acima de 40%", "expected": "supplier_concentration"},
        {"q": "Existem documentos duplicados?", "expected": "possible_duplicate_documents"},
        {"q": "Quais notas parecem duplicadas?", "expected": "possible_duplicate_documents"},
        {"q": "Ha lacunas na numeracao das notas?", "expected": "document_sequence_gaps"},
        {"q": "Mostre lacunas de numeração por série", "expected": "document_sequence_gaps"},
        {"q": "Itens de alto valor com NCM generico", "expected": "generic_ncm_high_value"},
        {"q": "Quais itens genéricos têm valor alto?", "expected": "generic_ncm_high_value"},
        {"q": "Entradas 1556 com icms positivo", "expected": "possible_credit_on_consumption_cfop"},
        {"q": "Mostre CFOPs 1556 e 2556 com crédito de ICMS", "expected": "possible_credit_on_consumption_cfop"},
        {"q": "quero analisar documentos, mas não sei qual verificação usar", "expected": None},
        {"q": "copie o resultado", "expected": None},
        {"q": "bom dia", "expected": None},
        {"q": "quero saber sobre imposto de renda da pessoa fisica", "expected": None},
        {"q": "Mostre top 5 cfops de saida", "expected": "movement_by_cfop"},
        {"q": "Qual o resumo do Bloco K?", "expected": "block_k_activity"},
        {"q": "Existe saída regular por CFOP em fevereiro?", "expected": "movement_by_cfop"},
        {"q": "Há algum documento cancelado em janeiro?", "expected": "cancelled_documents"},
    ]

    out: list[dict[str, Any]] = []
    for c in cases:
        if strategy == "deterministic":
            d = deterministic_route(c["q"], projection)
        elif strategy == "gemini":
            d = route_with_gemini(c["q"], projection)
        elif strategy == "prefilter":
            d = prefilter_then_gemini(c["q"], projection)
        elif strategy == "fallback":
            d = deterministic_fallback_gemini(c["q"], projection)
        else:
            raise ValueError(f"Estrategia desconhecida: {strategy}")

        analysis_id_hit = d.analysis_id == c["expected"]

        out.append(
            {
                "question": c["q"],
                "expected": c["expected"],
                "analysis_id": d.analysis_id,
                "parameters": d.parameters,
                "needs_clarification": d.needs_clarification,
                "out_of_catalog": d.out_of_catalog,
                "latency_ms": round(d.latency_ms, 2),
                "error": d.error,
                "provider_failure": d.provider_failure,
                "analysis_id_hit": analysis_id_hit,
                "end_to_end_hit": analysis_id_hit and not d.provider_failure,
                "strategy": d.strategy,
            }
        )

    return out


def summarize(results: list[dict[str, Any]], strategy: str) -> None:
    total = len(results)
    analysis_id_hits = sum(1 for r in results if r["analysis_id_hit"])
    end_to_end_hits = sum(1 for r in results if r["end_to_end_hit"])
    out_cat = sum(1 for r in results if r["out_of_catalog"])
    provider_failures = sum(1 for r in results if r["provider_failure"])
    clarify = sum(1 for r in results if r["needs_clarification"])
    avg_latency = sum(r["latency_ms"] for r in results) / total if total else 0
    analysis_id_accuracy = analysis_id_hits / total if total else 0.0
    end_to_end_success_rate = end_to_end_hits / total if total else 0.0

    print(f"\n=== STRATEGY: {strategy} ===")
    print(
        f"total_cases={total} analysis_id_hits={analysis_id_hits} analysis_id_accuracy={analysis_id_accuracy:.3f} "
        f"end_to_end_hits={end_to_end_hits} end_to_end_success_rate={end_to_end_success_rate:.3f} "
        f"out_of_catalog={out_cat} provider_failures={provider_failures} needs_clarification={clarify}"
    )
    print(f"avg_latency_ms={avg_latency:.2f}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Spike 2 - Comparacao de estrategias de roteamento de catalogo.")
    parser.add_argument(
        "--catalog",
        default="rfcs/agente-sped-fiscal/research/data/analysis_catalog.yml",
        help="Catalogo YAML.",
    )
    parser.add_argument(
        "--strategy",
        default="all",
        choices=["all", "deterministic", "gemini", "prefilter", "fallback"],
        help="Estrategia de execucao.",
    )
    parser.add_argument("--show-json", action="store_true", help="Imprime resultados detalhados em JSON.")
    parser.add_argument("--self-checks", action="store_true", help="Valida falha do provider e fallback sem chamada de rede.")
    return parser.parse_args()


def check(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run_self_checks(projection: dict[str, dict[str, Any]]) -> int:
    original_key = os.environ.pop("GOOGLE_API_KEY", None)
    try:
        direct = route_with_gemini("Mostre saídas por CFOP", projection)
        check(direct.provider_failure, "provider_failure esperado sem GOOGLE_API_KEY")
        check(not direct.out_of_catalog, "falha tecnica nao deve virar out_of_catalog")

        fallback = deterministic_fallback_gemini("bom dia", projection)
        check(fallback.provider_failure, "fallback deve preservar provider_failure sem chave")

        print("self_check_provider_failure: ok")
        return 0
    finally:
        if original_key is not None:
            os.environ["GOOGLE_API_KEY"] = original_key
        else:
            os.environ.pop("GOOGLE_API_KEY", None)


def main() -> int:
    args = parse_args()
    projection = load_routing_projection(args.catalog)

    if args.self_checks:
        return run_self_checks(projection)

    strategies = ["deterministic", "gemini", "prefilter", "fallback"] if args.strategy == "all" else [args.strategy]

    for s in strategies:
        res = evaluate_cases(s, projection)
        summarize(res, s)
        if args.show_json:
            print(json.dumps(res, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
