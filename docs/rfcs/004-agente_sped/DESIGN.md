# Design Architecture: Agente SPED Fiscal

## 🎯 Objetivo (Problema de Negócio)

O Agente SPED Fiscal permite que analistas executem análises rastreáveis sobre arquivos EFD ICMS/IPI (TXT) sem necessidade de mapeamento manual de posições e relacionamentos do leiaute.

O sistema opera com um **conjunto controlado e versionado de análises catalogadas**, executadas de forma **reproduzível** sobre um workspace local. A Inteligência Artificial atua estritamente como apoio ao roteamento de intenções e à explicação narrativa de resultados que já foram calculados de forma determinística. 

O produto auxilia a investigação técnica. Ele não substitui o Programa Validador e Assinador (PVA), não transmite escriturações e **não produz parecer fiscal ou jurídico conclusivo**.

## ⚙️ Escopo da Solução

* **Entrada:** Arquivo TXT EFD ICMS/IPI, intenção do usuário (linguagem natural ou seleção direta) e parâmetros de filtro.
* **Processamento Central:**
  1. Detectar o `COD_VER` e recusar versões sem suporte.
  2. Selecionar o catálogo de metadados correspondente.
  3. **Parsing estrutural preservando a identidade do arquivo, do registro e da linha de origem**, normalizando tipos (`raw_values` e `typed_values`).
  4. Reconstrução da hierarquia estrutural direta.
  5. Materialização no DuckDB e **geração do FileProfile**.
  6. Validação de disponibilidade baseada nos registros definidos pelo leiaute, campos exigidos, ocorrências reais e configuração `record_presence_required`.
  7. Roteamento de intenção com validação de contrato.
  8. Execução analítica restrita no DuckDB.
  9. Projeção de payload narrativo separado do resultado completo, mascarado e limitado.
  10. **Geração de resposta determinística ou sintetizada pelo provedor opcional de IA.**
* **Saída:** **Identificação e versão da análise, parâmetros efetivos, status, resultado estruturado, referências rastreáveis (file_id, record_id, parent_record_id, source_line), síntese textual e motivo explícito para pergunta fora do catálogo, necessidade de esclarecimento, indisponibilidade, resultado vazio ou falha.**
* **Fora do Escopo:** múltiplos arquivos por workspace; auditorias cruzadas; outras obrigações ou leiautes sem suporte; Text-to-SQL livre; análises autônomas criadas por IA; autenticação corporativa; multi-tenancy; API pública; processamento distribuído e transmissão ao fisco.

## 👥 Usuários e Papéis

* **Analista Fiscal:** Seleciona ou pergunta pela análise, ajusta parâmetros, inspeciona resultados baseados em evidências (linha de origem) e decide os próximos passos.
* **Desenvolvedor ou Operador Local:** Mantém os catálogos, o ambiente, as fixtures de teste e a configuração opcional do provedor de IA.
* **Provedor de IA (Opcional):** Apoia o roteamento estruturado e a narrativa; não calcula valores, cria análises ou executa SQL.

## 🧩 Modelo de Domínio e Dados

* **Premissas Arquiteturais:** O sistema **opera com um arquivo por workspace**. Ingestão, parsing, staging, cálculos e resultados completos permanecem no ambiente local. **Quando habilitado, o provedor externo recebe somente o payload narrativo permitido pelo Catálogo de Análises.** Regras e parâmetros são tratados por código determinístico, e as consultas catalogadas são executadas no DuckDB. A IA não recalcula nem altera valores. A superfície de interação é local, utiliza os mesmos serviços de aplicação e exige uma ação explícita do usuário para cada execução analítica.

### Contratos Estruturais
* **Workspace:** **Isola uma sessão analítica (workspace_id, file_id, versão do leiaute, caminho do DuckDB e status).**
* **FileProfile:** Perfil estruturado do arquivo, contendo período, contagens por registro e anomalias observadas.
* **ParsedRecord:** **Registro interpretado com identidade, código, nível, linha de origem, pai estrutural direto, valores brutos, valores tipados e erros de campo.**
* **AnalysisDefinition:** **Contrato versionado que reúne roteamento, disponibilidade, parâmetros, critérios, execução, resultado, privacidade, narrativa e limitações.**
* **NarrativePayload:** **Projeção reduzida e mascarada do resultado, contendo rows, total_rows, returned_rows, truncated, critérios e limitações permitidas para narrativa.**
* **AgentState:** **Mantém pergunta, rota, análise, parâmetros, resumo do resultado, payload narrativo, modo de narrativa e estados controlados como out_of_catalog, needs_clarification, provider_failure, analysis_unavailable, empty_result e execution_error.** Não armazena o TXT integral, relações completas, DataFrames, resultados irrestritos ou secrets.

### Schema versus Ocorrência
**Um registro definido pelo leiaute pode não ocorrer no arquivo. Uma relação DuckDB pode existir vazia para preservar o schema derivado do leiaute. A disponibilidade considera os campos exigidos, as ocorrências reais e record_presence_required; resultado vazio de uma análise válida não equivale a análise indisponível.**

## 🔌 Serviços de Aplicação e Integrações

* **Builders de Leiaute e Parser:** Transformam a fonte controlada em metadados operacionais e extraem o arquivo preservando identidade, linha de origem e pai estrutural direto, com recusa explícita de versões de leiaute sem suporte.
* **Workspace Analítico:** A ingestão materializa as relações antes da execução das análises. O executor opera somente sobre consultas catalogadas de leitura e não oferece operações de mutação do workspace ao fluxo de interação.
* **Roteador Analítico:** **O roteamento principal é determinístico. Em casos ambíguos ou de baixa confiança, o serviço pode consultar o provedor de IA; qualquer saída assistida é validada contra o Catálogo de Análises antes que a análise ou seus parâmetros sejam aceitos.**
* **Executor Analítico:** O executor aceita somente consultas de leitura declaradas no catálogo, processa uma instrução por execução, aplica parâmetros vinculados e limita o volume retornado. O controle reduz a superfície de execução, mas não deve ser tratado como sandbox absoluta.
* **Serviço de Narrativa:** **Constrói uma projeção limitada e mascarada do resultado. A resposta determinística permanece disponível sem provedor externo; a resposta assistida não pode recalcular, completar ou alterar os valores produzidos pelo executor.**

## 📡 Contratos de API e Integrações

Não há exposição de API REST pública ou webhooks assíncronos. A superfície HTTP é exclusiva da interface visual local e deve operar apenas em localhost (127.0.0.1/::1), sem exposição externa. A integração de rede ocorre pontualmente via SDK *outbound* com o provedor de IA (LLM), caso configurado.

## 🛡️ Segurança e Rastreabilidade

* **Mascaramento e Minimização:** O arquivo bruto e os resultados completos permanecem locais. O payload narrativo é limitado e as colunas declaradas como sensíveis são mascaradas antes de qualquer chamada externa. **Arquivos SPED reais, credenciais, arquivos .env, resultados completos e artefatos sensíveis de sessão não são versionados. Credenciais são fornecidas por variável de ambiente e não aparecem em logs.**

## ⚖️ Trade-offs Arquiteturais

* **TXT reprocessado sob demanda:** **Sem staging persistido, exige novo parsing e nova materialização para reconstruir o workspace**, aumentando o custo de parsing, restauração e consultas repetidas. Permanece como fonte canônica.
* **DuckDB materializado vs Parquet:** **DuckDB concentra o workspace e a execução analítica no mesmo motor, oferecendo restauração adequada ao cenário de um arquivo por workspace.** Parquet introduziria uma representação adicional sem benefício suficiente para o cenário atual, permanecendo elegível para intercâmbio futuro.
* **LLM Exclusivo:** O provedor é opcional. Torná-lo responsável exclusivo pelo roteamento eliminaria o funcionamento offline e acoplaria a disponibilidade do produto a um serviço externo.
* **Text-to-SQL livre:** Não adotado por reduzir a previsibilidade e **ampliar a superfície de execução de consultas não catalogadas**.

## 🧪 Observabilidade, Testes e DoD

* **Testes:** **Os testes devem cobrir leiaute não suportado, parsing e hierarquia, validação de parâmetros, disponibilidade, resultado vazio, resultados de referência das análises catalogadas, consultas não permitidas, mascaramento, truncamento, saída assistida inválida, roteamento determinístico e fallback sem provedor.**
* **Observabilidade:** **Logs registram identificadores técnicos, versão do leiaute, analysis_id, versão da análise, duração, quantidade de linhas, modo narrativo e erros higienizados.** Não registram secrets, arquivo bruto, resultados completos ou payloads sensíveis.
* **Definition of Done (DoD):** O parsing reconstrói a estrutura com tipagem válida. A materialização em DuckDB opera sem vazamento de dados analíticos sensíveis e processa com `status=ok` as consultas limitadas. O provedor externo, se ativado, interpreta e devolve intenções estruturadas sem acesso livre de leitura/escrita à base local.

## 📎 Referências

* **Manuais e Guias Práticos da EFD ICMS/IPI:** [Página oficial de índice do Portal SPED](https://sped.rfb.gov.br/pasta/show/1573).
* **Guia Prático vigente da EFD ICMS/IPI:** Acessado por meio da página oficial de Manuais e Guias Práticos.
* **Nota Técnica 2025.001:** Relacionada à vigência e adequações estruturais do leiaute `020`.
