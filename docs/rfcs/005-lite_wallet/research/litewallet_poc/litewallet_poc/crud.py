from sqlmodel import Session, select, SQLModel, create_engine
from model import AlocacaoDeRenda, Categoria, Gastos

engine = create_engine("sqlite:///database.db")


SQLModel.metadata.create_all(engine)



def create_alocacao_de_renda(alocacao):
    with Session(engine) as session:
        session.add(alocacao)
        session.commit()
        session.refresh(alocacao)
        return alocacao

def create_categoria(categoria):
    with Session(engine) as session:
        session.add(categoria)
        session.commit()
        session.refresh(categoria)
        return categoria


def create_gastos(gastos):
    with Session(engine) as session:
        session.add(gastos)
        session.commit()
        session.refresh(gastos)
        return gastos  

def select_gastos():
    with Session(engine) as session:
        statement = (
        select(Gastos, Categoria, AlocacaoDeRenda)
        .join(Categoria, Gastos.category_id == Categoria.id)
        .join(AlocacaoDeRenda, Gastos.alocacao_de_renda_id == AlocacaoDeRenda.id)
    )

        resultados = session.exec(statement).all()

        for gasto, categoria, alocacao in resultados:
            print(
                f"Gasto:'{gasto.name}',Valor:R$ {gasto.amount},Categoria:{categoria.name}'Alocação '{alocacao.name}"
                
            )
