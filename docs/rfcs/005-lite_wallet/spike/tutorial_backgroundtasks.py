# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi",
#     "uvicorn[standard]",
# ]
# ///
"""
MINI TUTORIAL: FastAPI BackgroundTasks
=======================================
Este arquivo é autossuficiente. Não depende de Celery, RabbitMQ nem de
nenhum outro arquivo deste repositório.

Como rodar:
    uv run tutorial_backgroundtasks.py

Depois acesse http://localhost:8000/docs para testar cada endpoint
pela interface do Swagger, e observe o terminal onde a API está rodando.
"""

import time
from datetime import datetime

from fastapi import BackgroundTasks, FastAPI

app = FastAPI(title="Tutorial - BackgroundTasks")


# ---------------------------------------------------------------------------
# 1. A tarefa mais simples possível: escrever em um "log" guardado em memória
# ---------------------------------------------------------------------------
execution_log: list[str] = []


def write_log(message: str) -> None:
    """
    Função comum, síncrona. Ela não sabe que está rodando "em segundo
    plano" — para ela, é só uma função normal sendo chamada.
    """
    timestamp = datetime.now().strftime("%H:%M:%S")
    execution_log.append(f"[{timestamp}] {message}")
    print(f"[{timestamp}] {message}")


@app.post("/log")
def create_log_entry(message: str, background_tasks: BackgroundTasks):
    """
    Devolve a resposta IMEDIATAMENTE e só depois executa write_log().
    """
    background_tasks.add_task(write_log, message)
    return {"status": "aceito", "mensagem": "log será escrito em segundo plano"}


@app.get("/log")
def read_log():
    """Só para conferir o que já foi escrito até agora."""
    return {"entradas": execution_log}


# ---------------------------------------------------------------------------
# 2. Uma tarefa "demorada", para sentir a diferença de tempo de resposta
# ---------------------------------------------------------------------------
def slow_task(job_id: str, duration: int) -> None:
    print(f"[{job_id}] iniciando tarefa de {duration}s...")
    time.sleep(duration)
    print(f"[{job_id}] tarefa concluída")


@app.post("/slow-task")
def run_slow_task(job_id: str, duration: int, background_tasks: BackgroundTasks):
    """
    Chame este endpoint e observe: a resposta HTTP volta na hora,
    mas o print de "concluída" só aparece no terminal depois de
    `duration` segundos — muito depois da conexão HTTP já ter fechado.
    """
    background_tasks.add_task(slow_task, job_id, duration)
    return {"status": "aceito", "job_id": job_id, "duration": duration}


# ---------------------------------------------------------------------------
# 3. Várias tarefas na mesma requisição — executam em SEQUÊNCIA, não em paralelo
# ---------------------------------------------------------------------------
def step_one(job_id: str) -> None:
    print(f"[{job_id}] passo 1")


def step_two(job_id: str) -> None:
    print(f"[{job_id}] passo 2")


@app.post("/multi-step")
def run_multi_step(job_id: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(step_one, job_id)
    background_tasks.add_task(step_two, job_id)
    return {"status": "aceito", "job_id": job_id}


# ---------------------------------------------------------------------------
# 4. O que acontece quando a tarefa falha? (sem tratamento nenhum)
# ---------------------------------------------------------------------------
def task_that_fails(job_id: str) -> None:
    print(f"[{job_id}] processando...")
    raise ValueError(f"[{job_id}] algo deu errado")


@app.post("/failing-task")
def run_failing_task(job_id: str, background_tasks: BackgroundTasks):
    """
    Dispare este endpoint e observe o terminal: a exceção aparece
    no log do servidor (traceback), mas:
      - o cliente HTTP já recebeu 202 e nunca fica sabendo do erro;
      - não existe retry automático;
      - não existe nenhum registro persistente do que falhou.
    """
    background_tasks.add_task(task_that_fails, job_id)
    return {"status": "aceito", "job_id": job_id}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
