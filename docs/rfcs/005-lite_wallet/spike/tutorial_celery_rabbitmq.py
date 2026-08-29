# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "celery",
# ]
# ///
"""
MINI TUTORIAL: Celery + RabbitMQ
=================================
Este arquivo é autossuficiente. Não depende de FastAPI, nem do
tutorial_backgroundtasks.py.

Pré-requisito: RabbitMQ rodando localmente.
    docker run -d --name rabbitmq -p 5672:5672 -p 15672:15672 rabbitmq:3-management

Como rodar (dois terminais):

Terminal 1 - subir o worker (quem EXECUTA as tarefas):
    uv run tutorial_celery_rabbitmq.py worker

Terminal 2 - produzir tarefas (quem ENFILEIRA o trabalho):
    uv run tutorial_celery_rabbitmq.py produce
"""

import sys
import time

from celery import Celery

# ---------------------------------------------------------------------------
# 1. App Celery: aponta para o "broker" (RabbitMQ), a fila de mensagens por
#    onde produtor e worker se comunicam sem se conhecerem diretamente.
# ---------------------------------------------------------------------------
celery_app = Celery(
    "tutorial_celery_rabbitmq",
    broker="amqp://guest:guest@localhost:5672//",
    backend="rpc://",  # onde o resultado da tarefa fica disponível para consulta
)


# ---------------------------------------------------------------------------
# 2. Uma tarefa simples: soma dois números (devagar, de propósito)
# ---------------------------------------------------------------------------
@celery_app.task
def add_numbers(a: int, b: int) -> int:
    print(f"somando {a} + {b}...")
    time.sleep(5)
    result = a + b
    print(f"resultado: {result}")
    return result


# ---------------------------------------------------------------------------
# 3. Uma tarefa que falha e se recupera sozinha (retry automático)
# ---------------------------------------------------------------------------
@celery_app.task(
    bind=True,
    autoretry_for=(ValueError,),
    retry_backoff=True,  # espera crescente entre tentativas: 1s, 2s, 4s...
    retry_kwargs={"max_retries": 3},
)
def flaky_task(self, job_id: str) -> str:
    print(f"[{job_id}] tentativa número {self.request.retries + 1}")
    if self.request.retries < 2:
        # falha propositalmente nas duas primeiras tentativas
        raise ValueError(f"[{job_id}] falha simulada")
    return f"[{job_id}] sucesso na tentativa {self.request.retries + 1}"


# ---------------------------------------------------------------------------
# 4. Produtor: quem manda a tarefa para a fila, sem executar nada localmente
# ---------------------------------------------------------------------------
def produce() -> None:
    print("Enviando add_numbers(2, 3) para a fila...")
    result = add_numbers.delay(2, 3)
    print(f"Task enviada, id={result.id}. O código continua rodando aqui,")
    print("sem esperar o worker terminar.")

    print("\nEnviando flaky_task('demo-1') para a fila...")
    flaky_result = flaky_task.delay("demo-1")
    print(f"Task enviada, id={flaky_result.id}.")

    print("\nAgora vá até o terminal do worker e observe a execução.")
    print("Se quiser esperar e ver o resultado por aqui, descomente as linhas abaixo:")
    print("# print(result.get(timeout=15))")
    print("# print(flaky_result.get(timeout=30))")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            """
Uso:
    uv run tutorial_celery_rabbitmq.py worker    # inicia o worker
    uv run tutorial_celery_rabbitmq.py produce   # envia tarefas para a fila
"""
        )
        raise SystemExit(1)

    mode = sys.argv[1]

    if mode == "worker":
        celery_app.worker_main(["worker", "--loglevel=info", "--concurrency=1"])
    elif mode == "produce":
        produce()
    else:
        print(f"Modo inválido: {mode}")
        raise SystemExit(1)
