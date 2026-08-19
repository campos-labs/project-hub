# LiteWallet — Extração estruturada de áudio

POC relacionada à issue **#43 — LiteWallet - Validação de extração estruturada de áudios - via LangGraph e Gemini**.

Este diretório contém o código experimental utilizado para validar a extração de informações de transações financeiras diretamente de arquivos de áudio.

A integração utiliza `langchain-google-genai` com Structured Output e Pydantic para transformar o conteúdo do áudio em uma estrutura `TransactionExtraction`.

> Este código é experimental e não representa implementação de produção.

## Requisitos

* Python 3.10 ou superior
* `uv` instalado
* acesso à API utilizada pelo `langchain-google-genai`
* variável de ambiente `GOOGLE_API_KEY` configurada

## Ambiente

Crie um ambiente virtual com `uv`:

```bash
uv venv
```

Ative o ambiente.

### Linux/macOS

```bash
source .venv/bin/activate
```

### Windows PowerShell

```powershell
.venv\Scripts\Activate.ps1
```

## Dependências

Esta POC não possui `requirements.txt`.

Instale manualmente as dependências necessárias utilizando `uv`:

```bash
uv pip install langchain-google-genai langchain pydantic typing-extensions
```

## Configuração da API key

A credencial não deve ser adicionada ao código ou versionada no repositório.

Configure a variável de ambiente `GOOGLE_API_KEY` antes da execução.

### Linux/macOS

```bash
export GOOGLE_API_KEY="<sua-api-key>"
```

### Windows PowerShell

```powershell
$env:GOOGLE_API_KEY="<sua-api-key>"
```

O `app.py` deve obter essa configuração diretamente do ambiente.

Exemplo:

```python
import os

api_key = os.environ["GOOGLE_API_KEY"]
```

## Arquivo de áudio

O experimento trata arquivos de áudio nos formatos:

* `.m4a`
* `.ogg`
* `.mp3`
* `.wav`

O MIME type é identificado a partir da extensão do arquivo antes do envio ao modelo.

O arquivo de áudio utilizado no experimento não precisa ser versionado no repositório.

## Execução

Informe no `app.py` o caminho de um arquivo de áudio disponível localmente.

Exemplo:

```python
audio_path = "/caminho/para/audio.m4a"
```

Com o ambiente configurado, execute:

```bash
uv run app.py
```

## Saída esperada

Quando o áudio representar uma transação financeira, o experimento tenta produzir uma estrutura `TransactionExtraction` contendo:

```text
amount
currency
establishment
items_or_products
transaction_type
date
message
```

Exemplo conceitual:

```text
TransactionExtraction
├── amount             = Decimal("8.79")
├── currency           = "BRL"
├── establishment      = "Bazar da Esquina"
├── items_or_products  = ["caneta", "lápis", "borracha"]
├── transaction_type   = "compra"
├── date               = "25/09/2026"
└── message            = "<transcrição do áudio>"
```

Quando o áudio não representar uma transação financeira, apenas `message` deve conter a transcrição. Os campos financeiros devem permanecer nulos ou vazios.

## Regras utilizadas no experimento

Para os cenários avaliados:

* quando nenhuma data é mencionada, é utilizada a data atual;
* quando a data contém dia e mês, mas não contém ano, é utilizado o ano vigente;
* quando um ano é explicitamente informado, ele é preservado;
* quando existe uma transação sem moeda explicitamente informada, `BRL` é utilizada;
* itens não mencionados resultam em uma lista vazia;
* estabelecimento não informado resulta em `null`;
* conteúdo sem transação não deve gerar informações financeiras.

A data e o ano vigentes são obtidos pela aplicação e fornecidos ao modelo como contexto.

## Estrutura

```text
<diretório-da-poc>/
├── README.md
└── app.py
```

O `README.md` documenta apenas como reproduzir o experimento.

A hipótese, os cenários avaliados, as evidências, limitações e a conclusão da POC são registrados no corpo da Pull Request correspondente.
