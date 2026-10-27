# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "celery",
#     "fastapi",
#     "uvicorn[standard]",
# ]
# ///

# TUTORIAL — SPIKE DE PROCESSAMENTO ASSÍNCRONO
# =============================================
#
# Objetivo
# --------
# Este script compara duas formas de executar trabalho depois do aceite de
# uma requisição HTTP: FastAPI BackgroundTasks e Celery + RabbitMQ. As tarefas
# são sintéticas e não representam o processamento real do LiteWallet.
#
# Pré-requisitos
# --------------
# Execute todos os comandos a partir da raiz do repositório. São necessários:
#
# * Python >= 3.11;
# * uv;
# * Docker, somente para o experimento Celery + RabbitMQ;
# * curl, para observar uma conexão HTTP real.
#
# O bloco PEP 723 no início deste arquivo declara as dependências Python. O uv
# cria o ambiente efêmero automaticamente; não é necessário criar um
# requirements.txt, compose.yaml ou outro arquivo de infraestrutura.
#
# RabbitMQ
# --------
# Em um terminal, inicie o broker:
#
#     docker run --detach \
#       --user 999:999 \
#       --name litewallet-spike-rabbitmq \
#       --publish 5672:5672 \
#       --publish 15672:15672 \
#       rabbitmq:3.13-management
#
# O usuário 999:999 corresponde a rabbitmq nessa tag. Ele evita que o cookie
# Erlang seja criado como root:root em ambientes Docker que não corrigem a
# propriedade do arquivo antes de iniciar o broker.
#
# Confirme que ele está pronto:
#
#     docker exec litewallet-spike-rabbitmq rabbitmq-diagnostics -q check_running
#
# A interface de gerenciamento fica em http://localhost:15672 (usuário e
# senha padrão: guest/guest). O broker é necessário apenas para os modos
# worker e produce.
#
# FastAPI BackgroundTasks
# -----------------------
# Em outro terminal, inicie somente a API:
#
#     uv run docs/rfcs/005-lite_wallet/spike/async_processing_comparison.py background
#
# A API não inicia RabbitMQ nem worker. Em um terceiro terminal, experimente:
#
#     curl -sS -w '\nstatus=%{http_code} total=%{time_total}s\n' \
#       -X POST 'http://localhost:8000/log?message=primeiro-log'
#     curl -sS 'http://localhost:8000/log'
#     curl -sS -w '\nstatus=%{http_code} total=%{time_total}s\n' \
#       -X POST 'http://localhost:8000/slow-task?job_id=slow-1&duration=5'
#     curl -sS -w '\nstatus=%{http_code}\n' \
#       -X POST 'http://localhost:8000/multi-step?job_id=steps-1'
#     curl -sS -w '\nstatus=%{http_code}\n' \
#       -X POST 'http://localhost:8000/failing-task?job_id=fail-1'
#
# Os quatro POSTs devem responder 202. Observe no terminal da API que o log
# de conclusão de slow-task aparece depois da resposta HTTP, que multi-step
# imprime passo 1 antes de passo 2 e que failing-task produz traceback sem
# retry automático. O processo da API é a própria fronteira de vida dessas
# tarefas: uma interrupção abrupta do processo pode descartar trabalho ainda
# não concluído.
#
# Celery worker e produtor
# ------------------------
# Deixe o broker em execução e inicie o worker em um segundo terminal:
#
#     uv run docs/rfcs/005-lite_wallet/spike/async_processing_comparison.py worker
#
# Em um terceiro terminal, publique as tarefas:
#
#     uv run docs/rfcs/005-lite_wallet/spike/async_processing_comparison.py produce
#
# O produtor publica add_numbers(2, 3) e flaky_task('demo-1') e termina sem
# esperar o worker. O worker imprime a soma, as tentativas da tarefa instável
# e os retries com backoff. A tarefa instável falha duas vezes e deve concluir
# na terceira tentativa; max_retries=3 é o limite configurado, não a contagem
# de falhas que este cenário necessariamente utiliza.
#
# Experimentos sugeridos
# ----------------------
# 1. Publique tarefas sem worker, encerre o produtor e só depois inicie o
#    worker. Isso testa o desacoplamento do produtor.
# 2. Pare e inicie o mesmo container RabbitMQ antes de consumir mensagens.
#    Registre somente o que for observado; remover o container é um cenário
#    diferente e pode apagar o armazenamento interno.
# 3. Interrompa o worker durante add_numbers e diferencie encerramento gracioso
#    de encerramento abrupto. Com o reconhecimento antecipado usado neste
#    exemplo, não há garantia de redelivery de uma tarefa já reconhecida.
# 4. Pare o broker e execute produce. A publicação deve falhar enquanto o
#    broker estiver indisponível; isso não é um aceite persistente.
# 5. Meça status HTTP e tempo de resposta com curl, e compare-os ao tempo
#    impresso pelas tarefas. TestClient isolado não substitui essa medição.
#
# O modo report imprime um resumo estático da validação. Ele não consulta um
# banco de evidências, não monitora RabbitMQ e não transforma comportamento
# esperado em resultado observado.
#
# Limpeza
# -------
# Encerre a API e o worker com Ctrl+C. Depois, remova apenas o container criado
# para esta Spike:
#
#     docker stop litewallet-spike-rabbitmq
#     docker rm --volumes litewallet-spike-rabbitmq
#
# A opção --volumes remove somente o volume anônimo criado junto com esse
# container. Não reutilize esse comando para containers ou volumes existentes.
#
# Para consultar a ajuda da CLI:
#
#     uv run docs/rfcs/005-lite_wallet/spike/async_processing_comparison.py --help

import argparse
import time
from collections.abc import Sequence
from datetime import datetime

from celery import Celery
from fastapi import BackgroundTasks, FastAPI

BROKER_URL = "amqp://guest:guest@localhost:5672//"
API_HOST = "0.0.0.0"
API_PORT = 8000

fastapi_app = FastAPI(title="Tutorial - processamento assíncrono")

celery_app = Celery(
    "async_processing_comparison",
    broker=BROKER_URL,
    backend="rpc://",
)
celery_app.conf.update(
    # Torna explícito que a mensagem é persistente enquanto estiver na fila.
    # Isso não garante a recuperação de uma tarefa já reconhecida pelo worker.
    task_default_delivery_mode="persistent",
    # Preserva o comportamento mínimo da Spike: o reconhecimento ocorre antes
    # da execução; uma política de redelivery exigiria outra decisão de projeto.
    task_acks_late=False,
)

execution_log: list[str] = []


def write_log(message: str) -> None:
    """Registra uma mensagem em memória e no terminal da API."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = f"[{timestamp}] {message}"
    execution_log.append(entry)
    print(entry)


@fastapi_app.post("/log", status_code=202)
def create_log_entry(
    message: str,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Aceita um log e agenda sua gravação após a resposta HTTP."""
    background_tasks.add_task(write_log, message)
    return {"status": "aceito", "mensagem": "log será escrito em segundo plano"}


@fastapi_app.get("/log")
def read_log() -> dict[str, list[str]]:
    """Retorna os logs produzidos pelo processo atual da API."""
    return {"entradas": execution_log}


def slow_task(job_id: str, duration: int) -> None:
    """Executa uma tarefa lenta para separar aceite e processamento."""
    started_at = time.perf_counter()
    print(f"[{job_id}] iniciando tarefa de {duration}s...")
    time.sleep(duration)
    elapsed = time.perf_counter() - started_at
    print(f"[{job_id}] tarefa concluída em {elapsed:.2f}s")


@fastapi_app.post("/slow-task", status_code=202)
def run_slow_task(
    job_id: str,
    duration: int,
    background_tasks: BackgroundTasks,
) -> dict[str, object]:
    """Aceita uma tarefa lenta e agenda sua execução em segundo plano."""
    background_tasks.add_task(slow_task, job_id, duration)
    return {"status": "aceito", "job_id": job_id, "duration": duration}


def step_one(job_id: str) -> None:
    """Executa o primeiro passo do experimento sequencial."""
    print(f"[{job_id}] passo 1")


def step_two(job_id: str) -> None:
    """Executa o segundo passo do experimento sequencial."""
    print(f"[{job_id}] passo 2")


@fastapi_app.post("/multi-step", status_code=202)
def run_multi_step(
    job_id: str,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Agenda duas tarefas; Starlette as executa na ordem de registro."""
    background_tasks.add_task(step_one, job_id)
    background_tasks.add_task(step_two, job_id)
    return {"status": "aceito", "job_id": job_id}


def task_that_fails(job_id: str) -> None:
    """Falha propositalmente para demonstrar ausência de retry."""
    print(f"[{job_id}] processando...")
    raise ValueError(f"[{job_id}] algo deu errado")


@fastapi_app.post("/failing-task", status_code=202)
def run_failing_task(
    job_id: str,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Aceita uma tarefa que falhará depois que a resposta for enviada."""
    background_tasks.add_task(task_that_fails, job_id)
    return {"status": "aceito", "job_id": job_id}


@celery_app.task
def add_numbers(a: int, b: int) -> int:
    """Soma dois números lentamente para facilitar a observação no worker."""
    print(f"somando {a} + {b}...")
    time.sleep(5)
    result = a + b
    print(f"resultado: {result}")
    return result


@celery_app.task(
    bind=True,
    autoretry_for=(ValueError,),
    retry_backoff=True,
    retry_jitter=False,
    retry_kwargs={"max_retries": 3},
)
def flaky_task(self, job_id: str) -> str:
    """Falha nas duas primeiras tentativas e conclui na terceira."""
    attempt = self.request.retries + 1
    print(f"[{job_id}] tentativa número {attempt}")
    if self.request.retries < 2:
        raise ValueError(f"[{job_id}] falha simulada")
    return f"[{job_id}] sucesso na tentativa {attempt}"


def produce_tasks() -> None:
    """Publica tarefas e encerra sem aguardar seus resultados."""
    print("Enviando add_numbers(2, 3) para a fila...")
    result = add_numbers.delay(2, 3)
    print(f"Task enviada, id={result.id}.")

    print("Enviando flaky_task('demo-1') para a fila...")
    flaky_result = flaky_task.delay("demo-1")
    print(f"Task enviada, id={flaky_result.id}.")
    print("O produtor terminou; observe a execução no terminal do worker.")


REPORT_ROWS = (
    (
        "HTTP 202 e execução posterior",
        "OBSERVADO",
        "Os quatro POSTs retornaram 202; slow-task terminou depois da resposta.",
    ),
    (
        "Tempo HTTP versus tempo de slow_task",
        "OBSERVADO",
        "curl retornou 202 em 0,486s para uma tarefa configurada para 2s.",
    ),
    (
        "Sequência de multi-step",
        "OBSERVADO",
        "O worker da API imprimiu passo 1 antes de passo 2.",
    ),
    (
        "Falha de BackgroundTasks",
        "OBSERVADO",
        "202 foi recebido; traceback apareceu depois e a API continuou ativa.",
    ),
    (
        "Retry de flaky_task",
        "OBSERVADO",
        "Falhou duas vezes, esperou 1s/2s e concluiu na terceira tentativa.",
    ),
    (
        "Produtor independente",
        "OBSERVADO",
        "Duas execuções encerraram; quatro mensagens ficaram para outro worker.",
    ),
    (
        "Reinício do broker",
        "OBSERVADO",
        "Duas tarefas persistentes permaneceram; filas RPC temporárias sumiram.",
    ),
    (
        "Disponibilidade do broker",
        "OBSERVADO",
        "produce falhou sem broker; o worker reconectou após a volta do serviço.",
    ),
    (
        "Interrupção do worker e persistência",
        "OBSERVADO COM RESSALVA",
        "Após SIGKILL, duas mensagens voltaram à fila e foram reexecutadas.",
    ),
)


def print_report() -> None:
    """Imprime o snapshot não persistente dos resultados da Spike."""
    print("RESULTADOS DA SPIKE — BACKGROUNDTASKS VS CELERY + RABBITMQ")
    for scenario, status, detail in REPORT_ROWS:
        print(f"- [{status}] {scenario}: {detail}")
    print("O relatório não consulta serviços nem substitui evidências de execução.")


def build_parser() -> argparse.ArgumentParser:
    """Cria a interface de linha de comando da Spike."""
    parser = argparse.ArgumentParser(
        description="Comparação entre BackgroundTasks e Celery + RabbitMQ."
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)
    subparsers.add_parser("background", help="inicia a API FastAPI")
    subparsers.add_parser("worker", help="inicia o worker Celery")
    subparsers.add_parser("produce", help="publica tarefas no RabbitMQ")
    subparsers.add_parser("report", help="imprime o resumo da validação")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Executa o modo solicitado na CLI."""
    args = build_parser().parse_args(argv)
    if args.mode == "background":
        import uvicorn

        uvicorn.run(fastapi_app, host=API_HOST, port=API_PORT)
    elif args.mode == "worker":
        celery_app.worker_main(
            [
                "worker",
                "--loglevel=info",
                "--concurrency=1",
                "--pool=solo",
            ]
        )
    elif args.mode == "produce":
        produce_tasks()
    elif args.mode == "report":
        print_report()


if __name__ == "__main__":
    main()


# RESULTADOS DA SPIKE — BACKGROUNDTASKS VS CELERY + RABBITMQ
# =============================================================
#
# A. CENÁRIOS AVALIADOS
# ---------------------
# * BackgroundTasks: aceite HTTP 202, execução posterior, tarefa lenta,
#   ordem sequencial e falha sem retry.
# * Celery + RabbitMQ: publicação independente, consumo por worker, soma lenta,
#   retry automático com backoff e cenários de indisponibilidade/interrupção.
# * Persistência: mensagens ainda na fila, reconhecimento antecipado e
#   diferença entre parar/reiniciar e remover o container do broker.
#
# B. RESULTADOS
# -------------
# Os estados abaixo são deliberadamente separados:
#
# * CONFIRMADO NO CÓDIGO: comportamento diretamente verificável no fonte.
# * OBSERVADO: somente deve ser usado depois de uma execução manual registrada.
# * ESPERADO: comportamento previsto pela configuração, ainda sem evidência
#   desta execução.
# * NÃO EXECUTADO: experimento que depende de serviço, processo ou observação
#   manual e não foi realizado.
#
# Execução realizada nesta consolidação:
#
# * OBSERVADO — /log, /slow-task, /multi-step e /failing-task retornaram 202;
#   GET /log retornou 200 após a tarefa de log terminar.
# * OBSERVADO — curl recebeu /slow-task em aproximadamente 0,486s para
#   duration=2, enquanto o worker imprimiu conclusão após 2,00s.
# * OBSERVADO — multi-step imprimiu passo 1 antes de passo 2.
# * OBSERVADO — failing-task produziu ValueError/traceback depois do 202 e o
#   processo Uvicorn continuou atendendo requisições; não houve retry.
# * OBSERVADO — add_numbers(2, 3) foi consumida pelo worker, retornou 5 e levou
#   aproximadamente 5,00s em cada execução.
# * OBSERVADO — flaky_task falhou nas tentativas 1 e 2, teve backoff de 1s e
#   2s e obteve sucesso na tentativa 3.
# * OBSERVADO — com o worker parado, uma execução do produtor deixou duas
#   mensagens prontas. Uma segunda execução, após o primeiro produtor já ter
#   encerrado, elevou a fila a quatro; outro worker consumiu as quatro.
# * OBSERVADO — duas mensagens prontas na fila celery permaneceram após parar
#   e iniciar o mesmo container RabbitMQ. O novo worker consumiu ambas.
# * OBSERVADO — as filas temporárias do backend RPC existentes antes da parada
#   desapareceram no reinício do broker; o backend não atuou como armazenamento
#   persistente de resultados.
# * OBSERVADO COM RESSALVA — o worker foi encerrado com SIGKILL durante
#   add_numbers. Nesta execução com pool solo, as duas mensagens ficaram
#   prontas novamente e um worker reiniciado reexecutou add_numbers e
#   flaky_task. Isso é uma observação deste teste e não uma garantia geral de
#   recuperação com task_acks_late=False.
# * OBSERVADO — produce sem broker terminou com
#   kombu.exceptions.OperationalError: [Errno 111] Connection refused.
# * OBSERVADO — um worker conectado detectou o desligamento do broker, tentou
#   reconectar com esperas crescentes e voltou a conectar após o broker estar
#   novamente disponível.
# * DIAGNÓSTICO — o erro inicial EACCES ocorreu porque .erlang.cookie tinha
#   modo 0400 e proprietário root:root, mas o broker executava como UID 999.
#   Executar a imagem fixada como 999:999 criou o cookie como 999:999 e iniciou
#   RabbitMQ 3.13.7 sem alterar volumes preexistentes.
# * NÃO EXECUTADO — remoção e recriação do container, acks tardios, dead-letter
#   queues, volumes externos e outras garantias de produção, por estarem fora
#   do escopo desta Spike.
#
# O valor de 0,486s é uma medição deste ambiente, não uma garantia de latência.
# Da mesma forma, o redelivery observado após SIGKILL não deve ser generalizado
# para outros pools, momentos de reconhecimento ou configurações do Celery.
#
# Evidências devem registrar, no mínimo: status HTTP, tempo de curl, horário ou
# ordem dos logs, quantidade de tentativas da flaky_task, ação de interrupção,
# estado do container RabbitMQ e versão das dependências utilizadas.
#
# C. COMPARAÇÃO TÉCNICA
# ---------------------
# * Desacoplamento da resposta: ambos aceitam o trabalho antes da conclusão;
#   BackgroundTasks continua dentro do processo da API, enquanto Celery
#   publica para um worker separado.
# * Isolamento: BackgroundTasks compartilha o ciclo de vida e recursos da API;
#   Celery separa produtor, broker e consumidor, ao custo de infraestrutura.
# * Continuidade: BackgroundTasks perde tarefas quando o processo da API é
#   encerrado antes da conclusão. Celery pode manter mensagens ainda na fila,
#   mas este exemplo não promete recuperação de tarefa já reconhecida, pois
#   task_acks_late=False.
# * Retry: BackgroundTasks não possui retry automático; flaky_task usa retry
#   explícito do Celery com duas falhas simuladas e backoff.
# * Infraestrutura: BackgroundTasks não requer serviço externo; Celery requer
#   RabbitMQ disponível e configuração operacional correspondente.
# * Complexidade: BackgroundTasks é menor para trabalho curto e não crítico;
#   Celery é mais adequado ao processamento demorado e sujeito a falhas, desde
#   que a implantação trate reconhecimento, idempotência e observabilidade.
#
# D. CONCLUSÃO ARQUITETURAL
# -------------------------
# A recomendação para a v1.0 é Celery + RabbitMQ, sustentada pelo isolamento
# do produtor, pela fila intermediária e pelo retry observável. A recomendação
# não significa que o exemplo garanta durabilidade ou recuperação em qualquer
# reinicialização: mensagens publicadas, mensagens reconhecidas e tarefas em
# execução têm comportamentos diferentes.
#
# Para produção, uma decisão posterior deverá avaliar acks tardios, redelivery,
# idempotência, volume persistente do broker, política de resultados e
# observabilidade. Esses itens não são implementados nesta Spike.
