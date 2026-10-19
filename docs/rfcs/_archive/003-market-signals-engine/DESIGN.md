# Design Architecture: Motor de Sinais de Mercado em Near Real-Time

## 🎯 Objetivo (Problema de Negócio)
Detectar ineficiências matemáticas em mercados esportivos baseados em *odds*, identificando oportunidades com Valor Esperado Positivo (+EV). A aplicação consome e compara probabilidades de fontes distintas em tempo quase real (*near real-time*), alertando assimetrias matemáticas.

## ⚙️ Escopo da Solução
* **In:** Consumo contínuo (*polling* assíncrono) de fontes de dados (Mock JSON na v1.0, APIs reais como extensão).
* **Core:** Resolução de entidades (nomes de equipes), extração de margem de lucro (*Devigging*) via Pandas/NumPy e cálculo de *Valor Esperado*.
* **Out:** Persistência analítica no PostgreSQL, cache operacional no Redis e emissão de alertas via API.
* **Fora do Escopo:** Operação autônoma de contas (Bot de apostas) e recomendação de apostas financeiras.

## 🧩 Modelo de Domínio e Persistência Híbrida
A solução utiliza **PostgreSQL** para dados de infraestrutura e **Redis** como cache de baixa latência e *broker*.

### Entidades Permanentes (PostgreSQL)
* `Bookmaker`: `id`, `name`, `api_adapter_class`, `is_reference`.
* `TeamAlias`: `id`, `canonical_name`, `external_name`, `provider_id`. (Mapeamento crítico para normalização).
* `OpportunityAudit`: Log consolidado de alertas para avaliação analítica.

### Entidades Voláteis e Broker (Redis)
* **Oportunidades Ativas:** Cache operacional no Redis com TTL curto configurável. O histórico consolidado dos sinais detectados é preservado na tabela `OpportunityAudit`.

## 🧮 Lógica de Domínio: Probabilidade, Devigging e Valor Esperado
Implementada em rotinas vetorizadas no **Pandas/NumPy**:
1. **Probabilidade Implícita:** Conversão das cotações decimais do mercado de referência.
2. **Devigging Proporcional:** Remoção da margem (*Juice*) da casa de referência para encontrar a probabilidade real nua.
3. **Simulação Teórica (Kelly Criterion):** Aplicação de fórmula educacional/teórica para dimensionamento fracionário de *bankroll* simulado, sem execução transacional.

## 🔌 Serviços de Aplicação e Integrações
* **`OddsProvider`:** *Factory Pattern* isolando a rede. A v1.0 utiliza o `MockOddsProvider` com *fixtures* realistas. Adaptadores reais (`PinnacleAdapter`, etc.) ficam previstos como extensão.
* **`TeamResolver`:** Carrega `TeamAlias` em cache na inicialização do worker e usa *RapidFuzz* para correspondência difusa. *Cache misses* geram pendência no PostgreSQL para revisão humana.

## 📡 Contratos de API e Workers
* **FastAPI:**
  * `GET /api/v1/opportunities/active` - Retorna os alertas armazenados no Redis.
  * `POST /api/v1/aliases/resolve` - Endpoint para o operador aprovar nomes ambíguos.
* **Taskiq Workers (Redis Broker):**
  * `fetch_reference_lines_task` - Processo central capturando o mercado base.
  * `scan_peripheral_lines_task` - Disparado concorrentemente para buscar ineficiências.

## 🛡️ Gestão de Limites e Resiliência
* **SLA de Cotação:** Dados obtidos com atraso superior a 30 segundos são marcados como *Stale* (Vencidos) e descartados do motor de cálculo.
* **Rate Limit e Circuit Breaker:** Adaptadores reais devem aplicar *backoff* exponencial e bloqueio temporário após falhas repetidas ou respostas HTTP 429.

## 🧪 Observabilidade e DoD
* **Testes (Pytest):** Testes no motor de *Devigging* injetando dataframes mockados para garantir precisão flutuante até a 4ª casa decimal.
* **Definition of Done (DoD):** Setup rodando via `docker-compose up` injetando dados do `MockOddsProvider`, detectando +EV e disponibilizando o alerta no endpoint FastAPI com latência controlada.

## 📎 Referências
* [The Odds API](https://the-odds-api.com/liveapi/guides/v4/) — dados de eventos, mercados e odds em formato JSON.
* [Pinnacle API](https://github.com/pinnacleapi/pinnacleapi-documentation) — documentação pública para estudo de integração com mercado de referência; uso real fica fora da v1.0 e depende de acesso, termos e disponibilidade.
* Artigos-base: [O que é uma aposta de valor?](https://www.pinnacle.com/betting-resources/pt/educational/what-is-a-value-bet/mdfj8gatuvw3m3mw), [Como calcular o valor esperado](https://www.pinnacle.com/betting-resources/pt/betting-strategy/how-to-calculate-expected-value/ees2ve46tm4htt32), [Valor da Linha de Fechamento (CLV)](https://www.pinnacle.com/betting-resources/pt/betting-strategy/how-do-we-measure-performance-and-true-skill-in-sports-betting/5xu2wcj8bhucdsxr) e [Como utilizar o Critério de Kelly](https://www.pinnacle.com/betting-resources/pt/betting-strategy/how-to-use-kelly-criterion-for-betting/2bt2lk6k2qwq7qj8).
* Fundamentos matemáticos: probabilidade implícita, devigging proporcional, valor esperado e Kelly Criterion fracionário.
