import base64
import mimetypes
from typing import List, Optional
from typing_extensions import TypedDict
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field
from langchain.messages import HumanMessage
from decimal import Decimal


class TransactionExtraction(BaseModel):
    amount: Optional[Decimal] = Field(default=None, description="Valor da transação mencionada no áudio.")
    currency: Optional[str] = Field(default=None, description="Moeda da transação mencionada no áudio.")
    establishment: Optional[str] = Field(default=None, description="Nome do estabelecimento ou local mencionado no áudio.")
    items_or_products: List[str] = Field(default_factory=list, description="Lista de itens ou produtos mencionados no áudio.")
    transaction_type: Optional[str] = Field(default=None, description="Descrição da natureza da compra ou venda, se aplicável.")
    date: Optional[str] = Field(default=None, description="Data da transação mencionada no áudio.")
    message: str = Field(description="Transcrição exata do que foi dito no áudio.")
    

class AudioState(TypedDict):
    audio_path: str
    mime_type: str
    resultado: TransactionExtraction | None

def get_mime_type(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    overrides = {".m4a": "audio/mp4", ".ogg": "audio/ogg", ".mp3": "audio/mpeg", ".wav": "audio/wav"}
    ext = "." + path.rsplit(".", 1)[-1].lower()
    return overrides.get(ext, mime or "application/octet-stream")
    

llm = ChatGoogleGenerativeAI(model="gemini-3.1-flash-lite", temperature=0, api_key="settings.GOOGLE_API_KEY")# digite a sua api key do google aqui
structured_llm = llm.with_structured_output(TransactionExtraction)


def processar_audio_curto(state: AudioState):
    from datetime import datetime
    with open(state["audio_path"], "rb") as f:
        audio_base64 = base64.b64encode(f.read()).decode("utf-8")

    now = datetime.now()
    current_year = datetime.now().year  

    

    texto_prompt = f"""Você é um assistente que transcreve áudios curtos e extrai informações estruturadas de transações financeiras.

    TAREFA:
    1. Transcreva o conteúdo do áudio fielmente no campo "message".
    2. Identifique se o áudio descreve uma transação financeira (compra, venda, pagamento, gasto, etc).

    SE HOUVER UMA TRANSAÇÃO:
    - Extraia valor, moeda, estabelecimento, itens/produtos, tipo de transação e data.
    - "items_or_products": liste apenas os itens explicitamente mencionados. Se nenhum item for citado, retorne uma lista vazia.
    - "currency": se não for mencionada, assuma "BRL".
    - "establishment": se não for mencionado, retorne null.
    - "date": 
    - Se nenhuma data for mencionada, use a data de hoje: {now.strftime('%d/%m/%Y')}.
    - Se a data mencionada não tiver o ano, use o ano atual: {current_year}.
    - Se o usuário mencionar um ano explicitamente, preserve esse ano, mesmo que seja diferente do atual.

    SE NÃO HOUVER UMA TRANSAÇÃO:
    - Preencha apenas o campo "message" com a transcrição literal.
    - Deixe todos os demais campos nulos ou vazios (não invente valores).

    Responda apenas com base no que foi efetivamente dito no áudio. Nunca invente informações que não foram mencionadas.
    """
        

    mensagem = HumanMessage(
        content=[
            {"type": "text", "text": texto_prompt},
            {
                "type": "media",
                "data": audio_base64,
                "mime_type": state["mime_type"],
            }
        ]
    )

    resposta = structured_llm.invoke([mensagem])
    return {"resultado": resposta}

audio_path = "audio_litewallet/datasets/registro.m4a"
resultado = processar_audio_curto({
    "audio_path": audio_path,
    "mime_type": get_mime_type(audio_path),
    "resultado": None
})
print(resultado["resultado"].model_dump())