from litewallet_poc.crud import create_gastos, select_gastos
from model import Gastos

def main():
    print("="*30)
    print("Bem-vindo ao LiteWallet POC")
    print('Escolha uma opção:')
    print('1. Adicionar gasto')
    print('2. Visualizar gastos')
    print('3. Sair')

    option = input("Digite o número da opção desejada: ")
    if option == '1':
        insert_gastos()
    elif option == '2':
        listar_gastos()
    elif option == '3':
        print("Saindo...")
    else:
        print("Opção inválida. Por favor, escolha uma opção válida.")
    print("="*30)


def insert_gastos():
    while True:
        name = input("Digite o nome do gasto: ")
        descricao = input("Digite a descrição do gasto: ")
        valor = float(input("Digite o valor do gasto: "))
        data = input("Digite a data do gasto (YYYY-MM-DD): ")
        category_id = int(input("Digite o ID da categoria: "))
        alocacao_de_renda_id = int(input("Digite o ID da alocação de renda: "))

        novo_gasto = Gastos(name=name, description=descricao, amount=valor, date=data, category_id=category_id, alocacao_de_renda_id=alocacao_de_renda_id)
        create_gastos(novo_gasto)

        print(novo_gasto)
        print(f"Gasto '{name}' criado com sucesso!")

        continuar = input("Deseja adicionar outro gasto? (s/n): ")
        if continuar.lower() != 's':
            break


def listar_gastos():
    gastos = select_gastos()
    if not gastos:
        print("Nenhum gasto cadastrado.")
    else:
        print("Gastos cadastrados:")
        for gasto in gastos:
            print(f"ID: {gasto.id}, Nome: {gasto.name}, Descrição: {gasto.description}, Valor: {gasto.amount}, Categoria ID: {gasto.category_id}, Alocação de Renda ID: {gasto.alocacao_de_renda_id}")


if __name__ == "__main__":
    main()