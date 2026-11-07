# Design Architecture: Simulador Analítico da Reforma Tributária

## 🎯 Objetivo (Problema de Negócio)
O `tax-reform-impact-simulator` transforma dados fiscais em estimativas de impacto tributário, resultado operacional e caixa. Permite revisar premissas, consultar evidências e distinguir resultados calculados, hipóteses e informações insuficientes.

## 📌 Premissas e Decisões
* **Ambiente:** distribuir o pacote Python como wheel da release, associado ao ambiente do Notebook. Fixar runtime e dependências; verificar compatibilidade com Delta, endpoint SQL e modelo semântico antes de atualizar versões.
* **Direct Lake:** adotar inicialmente tabelas físicas da Gold pelo SQL Analytics Endpoint, na modalidade `Direct Lake on SQL`. Registrar identidade efetiva e comportamento de fallback. Consultas que usem DirectQuery não contam como leitura Direct Lake comprovada; outra modalidade exige revisão explícita.

## ⚙️ Escopo da Solução
| Dimensão | Contrato da v1.0 |
| --- | --- |
| Recorte | Prestadores de serviços de São Paulo, operações domésticas e tratamentos expressamente suportados. A demonstração inicial cobre software, suporte e consultoria. |
| Empresa analisada | Fora do Simples; baseline de PIS/Cofins lido da EFD conforme o regime identificado e a matriz homologada. Fornecedores regulares ou optantes do Simples. |
| In | EFD-Contribuições TXT, NFS-e Nacional XML e eventos suportados; configuração de cenários e complementos tabulares revisados. |
| Core | Pré-validação, normalização, cálculo determinístico, persistência e rastreabilidade por execução. |
| Out | Comparação tributária, créditos conhecidos/potenciais, cobertura, detalhe documental, caixa simplificado e ponte gerencial quando houver base contábil suficiente. |
| Execução | Um CNPJ e uma competência-base por execução, em uma implantação organizacional. O acervo pode conter outras empresas, competências e execuções autorizadas. |
| Cenários | 2026, 2029 e 2033 como referências iniciais; participação hipotética do grupo de fornecedores elegíveis e alíquotas configuráveis com origem explícita. |
| Interface | Entrada assistida pelos recursos nativos do Fabric; consulta e sensibilidades executivas no Power BI. Sem frontend próprio obrigatório. |

* **Suporte:** a matriz da release identifica obrigações, leiautes, registros, eventos, tratamentos e limites de entrada testados. Aceitar a estrutura de um XML nacional não significa cobrir fiscalmente todos os municípios e serviços.
* **Fora do escopo:** comércio, indústria, operações mistas, regimes especiais e demais obrigações.

## 👥 Usuários e Papéis
* **Analista tributário:** prepara arquivos, revisa contexto e premissas, solicita a execução e avalia resultados.
* **Gestor:** consulta o relatório autenticado, seleciona cenários e acessa as evidências autorizadas.

### Fluxo operacional
1. **Preparar:** enviar o lote ao `Files` do Lakehouse e informar CNPJ, competência e configuração, sem alterar Python.
2. **Conferir:** revisar arquivos, duplicidades, versões, cobertura e premissas na pré-validação; inconsistências bloqueantes impedem o cálculo.
3. **Confirmar:** revisar parâmetros e complementos por grupo, registrar justificativa e solicitar a execução pelo Pipeline.
4. **Processar:** o Notebook chama os módulos do produto, registra a execução e publica resultados após os controles.
5. **Consultar:** selecionar empresa, competência, execução e cenário no Power BI, sem refazer ingestão ou cálculo.
6. **Revisar:** mudanças de insumos, regras ou premissas geram nova execução e preservam a anterior.

O `README.md` do repositório dedicado deverá documentar upload, configuração e acionamento nativos, com exemplos sintéticos. A revisão usará arquivo de configuração e tabela de complementos; não exigirá CRUD, aprovação individual de notas ou automação cadastral. O Power BI não será a tela de upload.

## 🧩 Modelo de Domínio e Dados

### Infraestrutura no Fabric
```text
TXT + XML/eventos + configuração/complementos
                    ↓
          OneLake / Lakehouse Files
                    ↓
       Fabric Pipeline → Notebook → pacote Python
                    ↓
       Bronze → Silver → Gold em Delta
                    ↓
       Modelo semântico Direct Lake → Power BI
```

### Camadas e granularidade
| Camada | Conteúdo e limites |
| --- | --- |
| Bronze | Arquivos originais, manifesto, hashes e linhas SPED. Divergências não autorizam reescrever as fontes. |
| Silver | Documentos, participantes, itens, eventos, vínculos e operações tipadas, preservando origem, ambiguidades, cancelamentos e rejeições. |
| Gold | Execuções, premissas efetivas, detalhe, resumo, cobertura, ponte gerencial, caixa e `source_trace`. Sem regras específicas de cartões ou gráficos. |

Preservar os contratos úteis da POC: `simulation_runs`, `simulation_detail`, `simulation_summary` e `source_trace`. Mudanças de schema exigem migração explícita. O resultado identifica execução, cenário, hipótese e operação; o resumo mantém empresa e competência.

### Entidades principais
* **Company / SourceFile:** identidade da empresa; arquivo, obrigação/tipo, versão, competência, hash e origem.
* **TaxRuleSet / Scenario:** regras e referências com vigência, versão e hash; hipóteses, unidade, ano projetado, política de base e autoria.
* **SimulationRun:** insumos, versões de código/parser/regras, premissas aplicadas, status, horários e validações.
* **Operation / SourceTrace:** identidade documental, situação, vínculos entre fontes, campo/bloco de origem e regra aplicada.
* **Resultado:** valores conhecidos, estimados e não determinados, separados por tratamento e cobertura.

**Identificadores:** armazenar como texto. A normalização de CNPJ preserva letras e aplica validação compatível com a fonte; não deve remover caracteres para manter apenas dígitos.

## 🔌 Serviços de Aplicação e Integrações

### Responsabilidades técnicas
| Componente | Responsabilidade |
| --- | --- |
| Python | Parsing, contratos, validações e funções fiscais testáveis, independentes da interface do relatório. |
| PySpark / Spark SQL | Transformações relacionais e materialização com schemas explícitos; substituir laços da POC onde houver benefício comprovável. |
| DuckDB | Verificações e regressões locais delimitadas; não é runtime alternativo do usuário nem segundo catálogo de regras. |
| Lakehouse / OneLake | Fontes preservadas, dados normalizados, resultados e histórico. |
| Pipeline / Notebook | Parâmetros, sequência, falhas e publicação. O Notebook importa o código da mesma release. |
| SQL Analytics Endpoint | Consulta das tabelas Delta; não é caminho de escrita do Lakehouse nem API transacional da solução. |
| Power BI / DAX | Consumo, navegação, indicadores e sensibilidades gerenciais delimitadas. |

### Leitura e proveniência
* **EFD:** usar `sped_layout_fields.csv` no parser, selecionando obrigação e versão. Preservar posições, hierarquia, `record_id` e `source_line`. Reutilizar lógica do agente SPED somente com equivalência demonstrada por contratos e testes.
* **NFS-e:** extrair por namespace e caminhos explícitos. O evento `101101` altera o estado analítico da nota, não seus bytes originais. Sinalizar evento sem documento, identificação ambígua e tipo não suportado, sem inventar associações.
* **XSD:** validar na ingestão com schemas homologados e versionados; carregar o validador uma vez por lote/versão, sem download ou extração de ZIP por documento. Registrar exceções conhecidas. Aprovação contra cópia alterada não é validação oficial; assinaturas sintéticas das fixtures não representam autorização fiscal.
* **RTC:** manter `rtc_service_rules.csv`, com build reproduzível das fontes oficiais selecionadas e registro de origem, versão, hash, vigência e regras derivadas. A correlação serviço/NBS/cIndOp/cClassTrib pode gerar candidatos: não escolher a primeira linha nem tratar a tabela como legislação completa. Calcular crédito presumido somente com tratamento implementado e homologado.

### Integração entre fontes
* **Precedência:** a EFD informa o baseline observado de PIS/Cofins; a NFS-e, o contexto documental, ISS e campos RTC disponíveis. Complementar a escrituração sem duplicar receita ou compra.
* **Vínculos:** usar chave exata e contexto compatível; alternativas exigem unicidade e conferência. Número de nota isolado não é identidade global. Expor divergências e ausências, sem forçar correspondência aproximada.
* **Cobertura:** incluir controles por empresa/competência e detalhe dos vínculos conhecidos, não reconciliação generalizada de escriturações, documentos e itens. Medir cobertura por quantidade e valor; priorizar revisão por materialidade, sem extrapolar amostras nem descartar operações silenciosamente. Agregar após respeitar tratamento e arredondamento.

### Regras, bases e precisão
* **Origem:** separar dado observado, cálculo determinístico e hipótese. O ano projetado não muda a competência dos arquivos. Alíquotas hipotéticas não recebem rótulo de oficiais; valores da POC não são parâmetros legais padrão.
* **Cenários:** definir política de base/preço, tributos legados, reduções, vigência e condições de crédito. Não aplicar alíquota futura ao bruto legado como se a base estivesse legalmente determinada. Novos anos e exceções exigem suporte e testes.
* **Precisão:** usar `Decimal`/`DECIMAL`, escala monetária 2 e escala intermediária e de percentuais de pelo menos 6, com arredondamento explícito por operação/tributo. Regras ficam no código e catálogo versionados; premissas operacionais, na configuração. Comparar contratos delimitados entre motores, não apenas totais coincidentes.

### Fornecedores e créditos
* **Regimes:** distinguir opção pelo Simples, recolhimento de IBS/CBS no período e hipótese de migração. Recolher IBS/CBS pelo regime regular não significa necessariamente sair do Simples.
* **Permanência:** aplicar o percentual à base monetária de um grupo de compras identificado, não à contagem de fornecedores ou à carteira inteira. Admitir premissas e complementos por grupo; automação individual e consultas cadastrais ficam para evolução.
* **Apropriação:** separar crédito conhecido/potencial de crédito apropriável no período. Ausência de destaque ou de condição necessária não significa crédito zero nem autoriza alíquota cheia. Comparar legado e potencial futuro sem concluir automaticamente direito a crédito ou erro na escrituração.
* **Apresentação:** distinguir crédito mantido, ampliação potencial, exclusão fundamentada e informação insuficiente. O líquido calculado só com créditos conhecidos é parcial; chamá-lo de limite superior exige demonstrar as demais condições desse limite.

### Resultado operacional e caixa
* **Ponte gerencial:** receber totais revisados de receita, deduções, custos e despesas em complemento tabular. Explicitar política bruto/líquido, mapeamento e cobertura, aproveitando os dados fiscais sem duplicação. Sem base suficiente, mostrar resultado consolidado indisponível, não lucro estimado sem sustentação. Não constitui DRE contábil completa ou leitura de ECD/ECF na v1.0.
* **Efeito contábil:** PIS/Cofins a recolher não é automaticamente despesa da DRE. Não reduzir custo e imposto duas vezes pelo mesmo crédito. Separar efeitos por competência de recolhimentos, retenções e prazo de apropriação.
* **Caixa:** projetar a necessidade tributária de forma simplificada, com saldos e defasagens informados. Preservar crédito postergado após o fechamento do mês. O tratamento especial do ano-teste depende das condições registradas, sem presumir dispensa para qualquer entrada. Não modelar fluxo financeiro completo, estoques, IRPJ/CSLL ou split payment.

### Fronteira Fabric/DAX
* **Fabric:** materializar resultados fiscais e cenários que exigem histórico ou consumo externo. DAX não decide classificação, direito a crédito, vigência ou base legal.
* **Interpolação:** permitir indicadores e hipóteses exploratórias sobre valores materializados apenas no intervalo e nas condições validadas. Limites, regimes, informação insuficiente e arredondamento podem invalidar a linearidade; não generalizar o Spike para qualquer carteira.
* **Repasse linear:** dividir o impacto selecionado entre parcelas repassada e absorvida, identificando base econômica ou de caixa. Não determina preço unitário nem garante compensar a tributação do novo preço. Valores negativos representam redução/benefício, não aumento.
* **Persistência:** hipóteses salvas como resultado da aplicação, consultadas por API ou que alterem cálculo fiscal devem executar no backend. O resultado fiscal essencial não depende de DAX.

## 📡 Contratos de Execução e Consumo

### Parâmetros e histórico
* **Configuração:** identificar lote, empresa, competência, regras e premissas. Manter valores digitados/importados separados dos fatos originais, com autoria e justificativa. Erros de entrada são apresentados ao analista, sem exigir edição do Notebook.
* **Reutilização:** a assinatura lógica inclui hashes dos insumos, regras, configuração e versão do código. Reutilizar execução concluída somente quando coincidirem. Recomposição forçada gera novo `run_id`, sem sobrescrever histórico.

### Publicação e falhas
* **Processamento:** operação inicial serial e controlada, sem escritores concorrentes independentes. Isolar gravações por execução e publicar a conclusão após os controles, sem presumir transação entre todas as tabelas Delta. Falhas não liberam resultados parciais nem apagam a última execução válida.
* **Disponibilidade:** o Pipeline publica os resultados e atualiza o enquadramento do modelo semântico. Distinguir processamento concluído de disponibilidade no BI; falha de atualização do relatório não exige recalcular impostos.

### Consulta no Power BI
* **Contexto:** mostrar empresa, competência, execução, cenário e cobertura; comparação tributária, fatores de crédito, ponte/caixa e detalhe rastreável. Explicar critérios e limitações com texto determinístico, sem exigir IA.
* **Filtros:** consultar o acervo sem iniciar Spark. Impedir somas de cenários ou execuções mutuamente exclusivos; comparar execuções somente com seleção explícita.
* **Limites e saídas:** mostrar empresas fora do recorte como não suportadas, sem promessa de “em breve” ou edição “Pro”. A exportação tabular autorizada preserva cenário e limitações. PDF, PPTX e relatório paginado não são obrigatórios.

## 🛡️ Segurança e Restrições
* **Governança:** aplicar [CONTRIBUTING](https://github.com/campos-labs/.github/blob/main/CONTRIBUTING.md) e [SECURITY](https://github.com/campos-labs/.github/blob/main/SECURITY.md). Higienizar definições e exemplos; dados reais não entram no Git.
* **Acesso:** autenticar por Entra, com menor privilégio e separação entre autoria/operação e leitura. Restringir por empresa quando necessário e testar relatório, modelo, endpoint SQL e OneLake. Slicer não é autorização; não conceder acesso amplo ao workspace a quem deve consultar apenas um subconjunto.

## 🧪 Observabilidade, Testes e DoD

### Operação e manutenção
* **Observabilidade:** registrar runtime, duração, volume, falhas e consumo observado. Documentar capacidade, licenças, retenção e recuperação. Trial não garante disponibilidade; não extrapolar SLA de segundos ou custo a partir das fixtures.
* **Referências:** acompanhar mudanças de regras/leiautes e seus impactos. Incorporar atualizações somente após revisão e regressão.

### Código e implantação
Criar os componentes conforme forem implementados, usando esta estrutura-alvo:
```text
tax-reform-impact-simulator/
├── README.md
├── DESIGN.md
├── pyproject.toml
├── src/tax_reform_impact_simulator/
│   ├── __init__.py
│   ├── efd_contribuicoes.py
│   ├── nfse_nacional.py
│   ├── rtc_rules.py
│   ├── normalization.py
│   └── simulation.py
├── data/reference/
├── tests/fixtures/
├── scripts/build_rtc_service_rules.py
└── fabric/
    ├── runtime.Environment/
    ├── run_simulation.Notebook/
    ├── tax_reform_simulation_pipeline.DataPipeline/
    ├── tax_reform_lakehouse.Lakehouse/
    ├── tax_reform_simulation_model.SemanticModel/
    └── tax_reform_impact_report.Report/
```
* **Versionamento:** a integração oficial gera as definições Fabric; configurar nomes e IDs de destino na implantação. Versionar código, ambiente, Pipeline, modelo e relatório, não os dados do OneLake. O PBIX da pesquisa é snapshot complementar, não backup autônomo do modelo Direct Lake.
* **Ciclo documental:** pesquisas permanecem no `project-hub`. Após promoção, este documento registra a RFC; o `DESIGN.md` do repositório dedicado orienta o produto. Capacidades futuras ficam na Epic; oportunidades incertas, na Discussion.

### Testes e aceite da release
* Um analista executa um novo lote suportado e altera premissas sem editar código; outro usuário autorizado consulta o resultado.
* Empresas e competências não misturam valores; cenários e execuções não são somados indevidamente.
* Duplicidade, cancelamento, versão incompatível, informação ausente e vínculo ambíguo têm comportamento verificado.
* Regras, bases, arredondamentos e agregados têm casos independentes; confrontar referências oficiais onde houver cobertura, sem pressupor validação já realizada.
* Reprocessamento preserva histórico; falhas não publicam resultados parciais. A execução pode ser reproduzida com os mesmos insumos e versões.
* Crédito incerto, ponte incompleta e caixa simplificado permanecem visíveis no relatório e nas exportações.
* Código e definições podem ser reimplantados; testes e documentação não dependem do link temporário da Trial.

## 📎 Referências
* [LC 214/2025 e texto atualizado](https://www2.camara.leg.br/legin/fed/leicom/2025/leicomplementar-214-16-janeiro-2025-796905-norma-pl.html) e [LC 227/2026](https://www2.camara.leg.br/legin/fed/leicom/2026/leicomplementar-227-13-janeiro-2026-798657-norma-pl.html); cada regra registra a norma e vigência aplicáveis.
* [NFS-e: documentação de produção](https://www.gov.br/nfse/pt-br/biblioteca/documentacao-tecnica/documentacao-atual), [RTC](https://www.gov.br/nfse/pt-br/biblioteca/documentacao-tecnica/rtc) e [implantações](https://www.gov.br/nfse/pt-br/biblioteca/documentacao-tecnica/atualizacoes-e-implantacoes).
* [EFD-Contribuições](https://www.gov.br/sped/pt-br/assuntos/escrituracoes-digitais/efd-contribuicoes), [CNPJ alfanumérico](https://www.gov.br/receitafederal/pt-br/acesso-a-informacao/acoes-e-programas/programas-e-atividades/cnpj-alfanumerico) e [regimes do Simples/IBS-CBS](https://www8.receita.fazenda.gov.br/simplesnacional/noticias/NoticiaCompleta.aspx?id=e595d010-1e04-4c3b-95d9-185fc58594b5).
* [Plataforma CBS e calculadora oficial](https://www.gov.br/receitafederal/pt-br/centrais-de-conteudo/publicacoes/manuais/reforma-tributaria-do-consumo/manual-plataforma-cbs-21-maio-2026-07h40.pdf): referência externa conforme a cobertura e versão disponíveis.
* [Direct Lake](https://learn.microsoft.com/en-us/fabric/fundamentals/direct-lake-overview), [SQL Analytics Endpoint](https://learn.microsoft.com/en-us/fabric/data-engineering/lakehouse-sql-analytics-endpoint) e [segurança](https://learn.microsoft.com/en-us/fabric/security/permission-model).
* [Definições Git do Fabric](https://learn.microsoft.com/en-us/fabric/cicd/git-integration/source-code-format) e [limites do PBIX](https://learn.microsoft.com/en-us/power-bi/create-reports/service-export-to-pbix).
