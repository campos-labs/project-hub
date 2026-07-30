# LiteWallet POC

LiteWallet POC é um projeto experimental para cadastro e visualização de gastos pessoais em um ambiente de terminal. A ideia é servir como prova de conceito para um futuro sistema de controle financeiro, usando Python, SQLModel e SQLite.

## O que o projeto faz

O aplicativo permite:

- cadastrar gastos com nome, descrição, valor, data e categorias;
- associar cada gasto a uma categoria e a uma alocação de renda;
- listar os gastos já salvos no banco de dados SQLite.

## Tecnologias utilizadas

- Python
- SQLModel
- SQLite
- Poetry

## Estrutura do projeto

- [litewallet_poc/main.py](litewallet_poc/main.py): ponto de entrada da aplicação em terminal.
- [litewallet_poc/crud.py](litewallet_poc/crud.py): operações de criação e leitura no banco de dados.
- [litewallet_poc/model.py](litewallet_poc/model.py): definição dos modelos SQLModel.
- [database.db](database.db): banco SQLite gerado automaticamente ao rodar o projeto.

## Requisitos

- Python 3.14 ou superior (conforme definido no arquivo de configuração)
- Poetry

## Instalação

Na raiz do projeto, crie o ambiente virtual com a versão desejada do Python:

```bash
poetry env use 3.14
```

Ative a virtualenv logo em seguida:

- Linux/macOS:

```bash
source .venv/bin/activate
```

- Windows (PowerShell):

```powershell
.\.venv\Scripts\Activate.ps1
```

- Windows (CMD):

```bat
.venv\Scripts\activate.bat
```

Em seguida, instale as dependências:

```bash
poetry install
```

## Como usar

A aplicação pode ser iniciada a partir da pasta do pacote:

```bash
cd litewallet_poc
poetry run python main.py
```

Ao executar, o programa exibe um menu com as opções:

1. Adicionar gasto
2. Visualizar gastos
3. Sair

## Observações

Este projeto é um POC simples, sem interface web ou autenticação. O foco atual é demonstrar a persistência de dados e o fluxo básico de cadastro e leitura de gastos.
