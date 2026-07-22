# Design Architecture: LiteWallet

## 🎯 Objetivo (Problema de Negócio)

A gestão financeira pessoal manual demanda tempo e gera atrito, resultando frequentemente no abandono do controle financeiro. O **LiteWallet** automatiza a captura, extração e categorização de despesas a partir de entradas não estruturadas (áudios e fotos de cupons/NFs). O sistema oferece ao usuário uma forma passiva, rápida e acessível de registrar gastos diários, centralizando e estruturando tudo em um repositório relacional consultável via REST API.

---

## 📌 Premissas e Decisões

1. **Orquestração Nativa via LangGraph:** A orquestração de todo o pipeline de ingestão, OCR, transcrição e extração de dados é feita de forma assíncrona utilizando o **LangGraph** integrado à aplicação Python.
2. **Suporte Multi-tenant Nativo:** Isolamento estrito de dados no banco PostgreSQL via `user_id` em todas as tabelas e consultas.
3. **Entrada Multimodal Nativa com Gemini:** Uso do modelo Gemini para OCR em cupons/NFs e transcrição/interpretação de áudios em uma única chamada com saída estruturada via `with_structured_output` e schemas Pydantic.
4. **FastAPI como REST API & Host de Execução:** O FastAPI atua como camada de serviços REST para as aplicações clientes e dispara a execução do grafo do LangGraph em segundo plano.
5. **Armazenamento de Arquivos:** Mídias temporárias são armazenadas no sistema de arquivos local ou Object Storage (MinIO/S3) para processamento pelos nós do grafo.

---

## ⚙️ Escopo da Solução

* **In:**
* Upload de imagens (JPG/PNG/PDF) contendo cupons fiscais e NFs.
* Upload de notas de voz/áudios (MP3/WAV/OGG) com descrições faladas de gastos.

* **Core:**
* Recebimento da mídia via FastAPI e acionamento assíncrono do **LangGraph**.
* Extração estruturada de dados com **Gemini** (valor, data, estabelecimento, itens e categoria).
* Tratamento de falhas, retentativas e validação sintática dentro dos nós do LangGraph.
* Persistência das transações categorizadas e normalizadas no PostgreSQL.

* **Out:**
* REST API consumível por clientes (Mobile/Web), fornecendo extratos, resumos por categoria e status de processamento das mídias.

* **Fora do Escopo:**
* Open Finance / Integração direta com instituições bancárias na v1.0.
* Interface gráfica (UI) inclusa neste repositório backend.

---

## 👥 Usuários e Papéis

* **Usuário Final (`User`):** Registra despesas enviando áudio/foto e consulta relatórios e status do processamento via API.
* **Motor de IA & Orquestração (`LangGraph + Gemini`):** Grafo encarregado de ler a mídia, aplicar OCR/transcrição, validar a estrutura de dados via Pydantic e persistir o resultado no banco.
* **API Server (`FastAPI`):** Gerencia autenticação, controle de permissões e disponibiliza os endpoints HTTP.

---

## 🧩 Modelo de Domínio e Dados

### Infraestrutura

* **PostgreSQL:** Banco de dados relacional OLTP para armazenamento seguro de usuários, categorias, transações e estado de execuções do LangGraph.
* **Local Storage / S3:** Armazenamento temporário de arquivos de mídia enviada.

### Entidades Principais

* `User`: `id`, `email`, `password_hash`, `created_at`.
* `Category`: `id`, `name`, `type` (EXPENSE/INCOME), `user_id`.
* `Transaction`: `id`, `user_id`, `category_id`, `amount`, `description`, `transaction_date`, `source_type` (VOICE/IMAGE/MANUAL), `status` (PENDING, PROCESSED, FAILED), `raw_metadata` (JSONB).

---

## 🔄 Fluxo do Grafo de Execução (LangGraph)

O fluxo de processamento de mídias é modelado como um grafo de estados no **LangGraph**:

```Text
[Start] ──> Node: IngestMedia (Upload & Validação)
               │
               ▼
        Node: ExtractWithGemini (OCR / Transcrição + Structured Output)
               │
               ▼
        Node: ValidateAndCategorize (Validação Pydantic + Mapeamento de Categoria)
               │
          ┌────┴──────────────┐
     (Sucesso)            (Falha/Retry)
          │                   │
          ▼                   ▼
Node: SaveTransaction   Node: HandleError (Marca PENDING/FAILED)
          │                   │
          └──────────┬────────┘
                     ▼
                  [End]

```

---

## 📡 Contratos de API (FastAPI)

### REST API Endpoints

* `POST /api/v1/auth/register` - Cadastro de novos usuários.
* `POST /api/v1/auth/login` - Autenticação JWT.
* `POST /api/v1/ingest/upload` - Recebimento de áudio ou imagem. Registra a entrada com status `PENDING` e dispara o LangGraph em segundo plano (`BackgroundTasks` ou worker assíncrono).
* `GET /api/v1/transactions` - Extrato de lançamentos filtrados por período e categoria.
* `GET /api/v1/transactions/{id}/status` - Consulta do status de processamento de um lançamento específico.
* `GET /api/v1/reports/summary` - Resumo financeiro consolidado.

---

## 🛡️ Segurança e Restrições

* **Autenticação:** JWT Bearer token em todas as rotas protegidas.
* **Isolamento Multi-tenant:** Filtro obrigatório por `user_id` em todas as consultas SQL e contextos do LangGraph.
* **Sanitização de Payload:** Schemas Pydantic validando rigorosamente os outputs gerados pelo LLM antes da gravação no banco.
* **Limites de Mídia:** Redimensionamento de imagens e limite de 10MB para áudio / 15MB para imagem.

---

## 🧪 Observabilidade, Testes e DoD

* **Testes:**
* **Unitários e de Integração:** Pytest para os endpoints do FastAPI e nós individuais do LangGraph.
* **Golden Files Dataset:** Suíte de testes com conjunto fixo de imagens e áudios de amostra (*golden dataset*) para auditar a precisão do OCR e da extração do Gemini no LangGraph.

* **Definition of Done (DoD):**
* Documentação OpenAPI/Swagger atualizada.
* Grafo LangGraph executando o fluxo completo de ponta a ponta (*Start* $\rightarrow$ *Gemini* $\rightarrow$ *PostgreSQL*).
* Suíte de testes automatizados rodando com sucesso no CI.

---
