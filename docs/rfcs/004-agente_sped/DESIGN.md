# Design Architecture: Agente SPED Fiscal

## 🎯 Objetivo (Problema de Negócio)

O sistema resolve a necessidade de consultar dados fiscais de um arquivo SPED Fiscal texto (.txt) usando linguagem natural. O usuário final é um analista fiscal ou desenvolvedor que quer extrair informações como valores faturados, ICMS, quantidades e outros indicadores diretamente do arquivo bruto, sem precisar mapear manualmente as colunas do SPED.

## 📌 Premissas e Decisões

* Produto formado por um agente de fluxo orientado a estados, baseado em **LangGraph**.
* Processamento local em memória usando **DuckDB**, sem persistência transacional ou banco de dados externo.
* Input inicial via CLI em loop (`agente_sped/main.py`) com arquivo fixo `data/exemplo_sped.txt`.
* LLM configurado para Google Gemini por meio de `langchain-google-genai`.
* V1 não inclui UI web, autenticação de usuários ou serviço de orquestração distribuída.
* Arquitetura single-tenant e single-file para análise de SPED.

## ⚙️ Escopo da Solução

* **In:** pergunta do usuário via terminal e caminho do arquivo SPED (`sped_file_path` fixo).
* **Core:** fluxo de três nós do grafo:
  1. `LLM_GENERATE_SQL` — gera SQL a partir do prompt especializado.
  2. `DUCKDB_EXECUTE_QUERY` — executa a query em um DuckDB em memória usando `read_csv` com parâmetros seguros.
  3. `LLM_FORMAT_RESPONSE` — formata e explica o resultado para o usuário.
* **Out:** resposta textual técnica ao usuário e, internamente, resultados JSON retornados pelo DuckDB.
* **Fora do Escopo:** interfaces REST, pipelines de ingestão, persistência de histórico, multi-arquivos simultâneos, classificação/ML adicional, backend distribuído.

## 👥 Usuários e Papéis

* **Analista fiscal / usuário local:** faz perguntas em linguagem natural sobre o arquivo SPED.
* **Desenvolvedor / operador:** mantém o ambiente Python/Poetry e a configuração de API do LLM.
* **LLM provider:** o serviço Google Gemini (`google-genai`) acessado via `GOOGLE_API_KEY`.
* **DuckDB:** executor SQL embutido responsável por ler e processar o arquivo SPED.

## 🧩 Modelo de Domínio e Dados

### Infraestrutura Local

* **DuckDB em memória** (`duckdb.connect(database=':memory:')`).
* **Arquivo SPED** lido como CSV delimitado por pipe usando `read_csv(..., delim='|', header=False, all_varchar=True, sample_size=-1, null_padding=True)`.
* **State Graph** baseado em `SPEDAgentState` do `langgraph.graph`.

### Entidades Principais

* `SPEDAgentState`:
  * `messages`: histórico de mensagens do diálogo e tool calls.
  * `sped_file_path`: caminho para o arquivo SPED.
  * `generated_sql`: SQL gerado pela LLM.
  * `duckdb_result`: resultado da query em JSON.
  * `sql_error`: erro retornado pela execução do DuckDB.

### Máquina de Estados

* `START` → `LLM_GENERATE_SQL`
* `LLM_GENERATE_SQL` → condicional:
  * se há tool call → `DUCKDB_EXECUTE_QUERY`
  * caso contrário → `LLM_FORMAT_RESPONSE`
* `DUCKDB_EXECUTE_QUERY` → `LLM_GENERATE_SQL` (loop de autocorreção / próxima iteração)
* `LLM_FORMAT_RESPONSE` → `END`

## 🔌 Serviços de Aplicação e Integrações

* `agente_sped.main.run_agent_cli()` — fluxo de interação CLI.
* `agente_sped.agent_.graph.graph` — orquestra o fluxo em LangGraph.
* `agente_sped.agent_.nodes.generator.generate_sql_node` — gera SQL com prompt especializado.
* `agente_sped.agent_.nodes.executor.execute_query_node` — executa tool `execute_duckdb_query`.
* `agente_sped.agent_.nodes.formatter.format_response_node` — formata a resposta final.
* `agente_sped.database.duckdb_client.execute_duckdb_query` — ferramenta DuckDB segura.
* `agente_sped.config.llm_config.call_llm()` — inicializa modelo Gemini.

## 📡 Contratos de API e Workers

Este projeto não expõe APIs REST formais nem workers assíncronos no momento.

Fluxo de interação:

* Usuário digita pergunta no CLI.
* `main.py` cria `initial_state` e invoca `graph.invoke(initial_state)`.
* O grafo processa estados e retorna o `final_state`.
* A resposta final é exibida em terminal.

Componentes funcionais:

* `execute_duckdb_query(sped_file_path, sql_query)` — tool chamada pela LLM para executar SQL.
* `graph.invoke()` — ponto de execução da aplicação.

## 🛡️ Segurança e Restrições

* `duckdb_client.py` sanitiza o SQL antes da execução:
  * corrige aspas duplas para aspas simples em comparações.
  * força parâmetros seguros no `read_csv(...)`, ignorando qualquer versão enviada pelo prompt.
* `Settings` carrega `GOOGLE_API_KEY` de `.env`.
* Não existe autenticação de usuário nem autorização granular nesta versão.
* O arquivo SPED é processado localmente, reduzindo exposição externa.

## 🧪 Observabilidade, Testes e DoD

* Atualmente não há casos de teste implementados no diretório `tests/` além de `__init__.py`.
* Estratégia recomendada para v1:
  * `pytest` para validar:
    * geração de SQL correta a partir de prompts.
    * execução DuckDB de exemplos SPED locais.
    * fluxo de reinjeção de erro/correção de query.
* DoD:
  1. Usuário pergunta em linguagem natural e recebe resposta técnica correta.
  2. O agente gera SQL e executa o DuckDB para qualquer consulta de dados.
  3. Erros de query são capturados e não geram falha fatal no CLI.
  4. Documento `README.md` e `DESIGN.md` atualizados com arquitetura vigente.
  5. Dependências e script `agente-sped` funcionam via `poetry install` + `poetry run agente-sped`.

## 📎 Referências

* `pyproject.toml` — dependências e scripts de execução.
* `agente_sped/main.py` — entrypoint CLI.
* `agente_sped/agent_/graph.py` — definição do fluxo LangGraph.
* `agente_sped/database/duckdb_client.py` — execução segura do DuckDB.
* `agente_sped/agent_/prompts/generator_prompt.py` — regras de geração de SQL para SPED.
* `agente_sped/agent_/prompts/formatter_prompt.py` — mensagem para formatação da resposta final.
