from typing import Optional
from sqlmodel import Field, Relationship, SQLModel, create_engine


engine = create_engine("sqlite:///database.db")

class AlocacaoDeRenda(SQLModel, table=True):
    __tablename__ = "alocacaoderenda"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: Optional[str] = None

    # Relacionamento inverso (opcional, para acessar os gastos a partir da alocação)
    gastos: list["Gastos"] = Relationship(back_populates="alocacao_de_renda")


class Categoria(SQLModel, table=True):
    __tablename__ = "categoria"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: Optional[str] = None

    # Relacionamento inverso (opcional, para acessar os gastos a partir da categoria)
    gastos: list["Gastos"] = Relationship(back_populates="categoria")


class Gastos(SQLModel, table=True):
    __tablename__ = "gastos"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: Optional[str] = None
    amount: float

    category_id: int = Field(foreign_key="categoria.id")
    alocacao_de_renda_id: int = Field(foreign_key="alocacaoderenda.id")

    # Mapeamento dos relacionamentos para facilitar navegação via código
    categoria: Optional[Categoria] = Relationship(back_populates="gastos")
    alocacao_de_renda: Optional[AlocacaoDeRenda] = Relationship(
        back_populates="gastos"
    )


SQLModel.metadata.create_all(engine)