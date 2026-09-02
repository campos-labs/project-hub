import warnings
import logging
import base64
import sys
from decimal import Decimal, InvalidOperation
from pydantic import BaseModel, Field, field_validator
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.messages import HumanMessage
from settings import Settings
from rich import print

warnings.filterwarnings("ignore", message=".*automatic function calling.*")
logging.getLogger("google_genai").setLevel(logging.ERROR)

settings = Settings()


class ItemCupom(BaseModel):
    descricao: str
    quantidade: float | None = None
    valor_unitario: Decimal | None = None
    valor_total: Decimal | None = None

    @field_validator("valor_unitario", "valor_total", mode="before")
    @classmethod
    def parse_decimal(cls, v):
        if v is None:
            return None
        try:
            # Converte via str primeiro para evitar imprecisão de binário
            # (ex.: Decimal(24.9) pode virar 24.899999999999999...)
            return Decimal(str(v))
        except InvalidOperation:
            raise ValueError(f"Valor monetário inválido: {v!r}")


class CupomFiscal(BaseModel):
    estabelecimento: str | None = None
    cnpj: str | None = None
    data_compra: str | None = None
    itens: list[ItemCupom] = Field(default_factory=list)
    valor_total: Decimal | None = None

    @field_validator("valor_total", mode="before")
    @classmethod
    def parse_decimal(cls, v):
        if v is None:
            return None
        try:
            return Decimal(str(v))
        except InvalidOperation:
            raise ValueError(f"Valor monetário inválido: {v!r}")


def encode_image(path: str) -> str:
    try:
        with open(path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode("utf-8")
    except FileNotFoundError:
        print(f"Erro: arquivo não encontrado em '{path}'", file=sys.stderr)
        raise
    except OSError as e:
        print(f"Erro ao ler o arquivo '{path}': {e}", file=sys.stderr)
        raise


def main():
    image_path = "cupom.jpeg"
    image_base64 = encode_image(image_path)

    llm = ChatGoogleGenerativeAI(
        model="gemini-3.1-flash-lite",
        temperature=0,
        api_key=settings.GOOGLE_API_KEY,
    )

    structured_llm = llm.with_structured_output(CupomFiscal)

    message = HumanMessage(
        content=[
            {
                "type": "text",
                "text": """
                Analise este cupom fiscal.

                Extraia:
                - estabelecimento
                - CNPJ
                - data da compra
                - produtos
                - quantidade
                - valor unitário
                - valor total de cada produto
                - valor total da compra

                Retorne somente as informações encontradas.
                Não invente informações que não estejam visíveis.
                """,
            },
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{image_base64}",
                },
            },
        ]
    )

    try:
        response = structured_llm.invoke([message])
    except Exception as e:
        print(f"Erro ao chamar o modelo: {e}", file=sys.stderr)
        raise

    print(response)


if __name__ == "__main__":
    main()