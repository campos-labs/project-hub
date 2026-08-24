# OCR LiteWallet

POC para validar a extração estruturada de dados financeiros a partir de imagens de **cupons fiscais**, utilizando **LangChain**, **Gemini Multimodal** e **Pydantic**.

## Objetivo

Validar se o Gemini consegue interpretar diretamente uma imagem de cupom fiscal e retornar seus dados em uma estrutura Pydantic, sem a necessidade de uma etapa separada de OCR tradicional.

O fluxo validado é:

```text
Imagem do cupom
      ↓
ChatGoogleGenerativeAI
      ↓
Gemini Multimodal
      ↓
Structured Output
      ↓
CupomFiscal (Pydantic)
```

## Estrutura

```text
ocr_litewallet/
├── datasets/
│   └── cupom.jpeg
├── main.py
├── settings.py
└── README.md
```

* `main.py`: execução da POC, schemas Pydantic e chamada ao Gemini.
* `settings.py`: carregamento das configurações e credenciais.
* `datasets/`: imagens utilizadas para validação.

## Variáveis de ambiente

Configure a chave da API do Google em um arquivo `.env`:

```env
GOOGLE_API_KEY=sua_chave_aqui
```

> O arquivo `.env` não deve ser versionado.

## Executando a POC

A partir da raiz do repositório:

```bash
python docs/rfcs/005-lite_wallet/research/ocr_litewallet/main.py
```

Ou entre no diretório da POC:

```bash
cd docs/rfcs/005-lite_wallet/research/ocr_litewallet
python main.py
```

## Dados extraídos

O schema `CupomFiscal` estrutura informações como:

* estabelecimento;
* CNPJ;
* data da compra;
* itens comprados;
* quantidade;
* valor unitário;
* valor total por item;
* valor total da compra.

Os valores monetários são representados utilizando `Decimal`.

## Exemplo de resultado

```text
CupomFiscal(
    estabelecimento='SUPERMERCADO "COMPRA FÁCIL"',
    cnpj='12.345.678/0001-00',
    data_compra='26/10/2023',
    itens=[
        ItemCupom(
            descricao='ARROZ TIO JOÃO 5KG',
            quantidade=1.0,
            valor_unitario=Decimal('24.9'),
            valor_total=Decimal('24.9')
        ),
        ...
    ],
    valor_total=Decimal('130.85')
)
```

## Resultado da validação

A POC demonstrou que o **Gemini Multimodal consegue interpretar uma imagem de cupom fiscal e retornar os principais dados financeiros diretamente em um objeto Pydantic estruturado**.

A abordagem elimina, para este cenário inicial, a necessidade de executar previamente uma ferramenta tradicional de OCR.

## Limitações

Esta é uma prova de conceito e não representa um fluxo pronto para produção.

A precisão da extração pode variar conforme fatores como:

* qualidade e resolução da imagem;
* legibilidade do cupom;
* diferentes layouts de documentos fiscais;
* latência e disponibilidade da API do Gemini;
* capacidade do modelo de interpretar corretamente os dados apresentados.

Validações adicionais e tratamento de cenários de borda deverão ser avaliados em etapas posteriores.
