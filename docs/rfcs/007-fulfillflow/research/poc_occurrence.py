# /// script
# requires-python = ">=3.13,<3.14"
# dependencies = [
#   "fastapi==0.141.1", "authlib==1.9.1", "psycopg[binary]==3.3.4",
#   "uvicorn==0.52.4", "jinja2==3.1.6", "python-multipart==0.0.32",
#   "itsdangerous==2.2.0", "httpx==0.28.1", "beautifulsoup4==4.15.0",
# ]
# ///
"""POC local da jornada de ocorrências do FulfillFlow 2.0.

Hipótese: OIDC, autorização por operação e alteração/histórico atômicos podem
compor uma jornada pequena, sem alterar o estado da remessa ou do pedido.

PowerShell, a partir da raiz do project-hub (Docker Desktop Linux e uv):
  $poc = 'docs/rfcs/007-fulfillflow/research/poc_occurrence.py'
  uv run $poc up
  uv run $poc verify
  # Opcional: --output <arquivo-novo.json> preserva uma nova execução.
  uv run $poc credentials
  uv run $poc serve
  # Abra http://127.0.0.1:8740. Ana/Bruno operam; Leitor apenas consulta.
  # Em outro terminal, com Ana autenticada: revogação na próxima operação.
  uv run $poc role ana viewer
  uv run $poc role ana operator
  # Ctrl+C encerra a aplicação. Pare apenas as dependências desta POC:
  uv run $poc stop

Escolhas provisórias: Keycloak real, Authorization Code + PKCE S256, Authlib;
sessões opacas e papéis locais no PostgreSQL, sem papéis no cookie. A autorização
é consultada em cada operação; revogação confirmada no banco impede novas
operações. Uma operação já autorizada mantém seu lock até terminar. Não se
avalia propagação de bloqueio no IdP, federação externa ou várias réplicas.

POSTs exigem sessão e CSRF. Somente o responsável pode encerrar; versão esperada
detecta concorrência. operation_id identifica a intenção, repetível com conteúdo
idêntico. Histórico guarda identidade interna ligada a iss/sub, ação e momento.
Seeds são sintéticos; o schema SQL explícito é exclusivo desta pesquisa, sem
substituir as migrations da aplicação. Não há testes em memória ou SQLite.

Integração de referência: campos-labs/fulfillflow em
9e3a135a00db218643633c7165d3106f0c8285e1. A ocorrência seria responsabilidade do
Core. Encerrar não altera Shipment/Order nem emite shipment.status_changed.v1.
Uma futura notificação exige fato occurrence.closed.v1, identidade própria e
outbox na mesma transação da ocorrência/histórico. Envio externo não é testado.

Limites: somente loopback HTTP, usuários sintéticos, Keycloak start-dev, sem
HA, carga, migração de dados ou integração com o FulfillFlow completo. Não é
configuração de produção. Credenciais aleatórias e realm gerado ficam no
diretório temporário indicado por `up`, fora do repositório. `stop` preserva
containers e dados; `up` retoma recursos com o identificador desta POC.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from http.cookiejar import CookieJar, DefaultCookiePolicy
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
import psycopg
import uvicorn
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.starlette_client import OAuth
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, StrictUndefined
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

BASE = "http://127.0.0.1:8740"
ISSUER = "http://127.0.0.1:8741/realms/fulfillflow-poc"
LABEL = "campos-labs.fulfillflow-occurrence-poc"
STATE = Path(tempfile.gettempdir()) / "fulfillflow-occurrence-poc"
IMAGES = {
    "db": "postgres:18-trixie@sha256:74935e72241653ca55e0414067e6d8763aceb8a810eb51b452253ec3dcfc4336",
    "idp": "quay.io/keycloak/keycloak:26.8.1@sha256:79c623a22abacf3fe6c00a5fb30f5b7843d717b234033b80995891c06116d244",
}
USERS = {"ana": "operator", "bruno": "operator", "leitor": "viewer"}
DEMO_ID = uuid5(NAMESPACE_URL, "fulfillflow:poc:occurrence:demo")
REFERENCE = "9e3a135a00db218643633c7165d3106f0c8285e1"


def user_id(name: str) -> UUID:
    return uuid5(NAMESPACE_URL, "fulfillflow:poc:identity:" + name)


def config() -> dict[str, str]:
    return json.loads((STATE / "config.json").read_text(encoding="utf-8"))


def connect(cfg: dict[str, str]) -> psycopg.Connection:
    return psycopg.connect(
        host="127.0.0.1",
        port=8742,
        dbname="occurrence_poc",
        user="poc",
        password=cfg["db_password"],
        connect_timeout=5,
        row_factory=dict_row,
        options="-c statement_timeout=5000 -c lock_timeout=3000",
    )


SCHEMA = """
CREATE TABLE IF NOT EXISTS principal (
 id uuid PRIMARY KEY, issuer text NOT NULL, subject text NOT NULL,
 name text NOT NULL, role text NOT NULL CHECK (role IN ('operator','viewer')),
 UNIQUE (issuer,subject)
);
CREATE TABLE IF NOT EXISTS browser_session (
 token_hash text PRIMARY KEY, principal_id uuid NOT NULL REFERENCES principal(id),
 expires_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS occurrence (
 id uuid PRIMARY KEY, shipment text NOT NULL, order_ref text NOT NULL,
 shipment_status text NOT NULL DEFAULT 'IN_TRANSIT',
 order_status text NOT NULL DEFAULT 'PROCESSING', reason text NOT NULL,
 state text NOT NULL DEFAULT 'OPEN' CHECK (state IN ('OPEN','IN_PROGRESS','CLOSED')),
 owner_id uuid REFERENCES principal(id), version integer NOT NULL DEFAULT 0,
 CHECK ((state='OPEN' AND owner_id IS NULL) OR
        (state IN ('IN_PROGRESS','CLOSED') AND owner_id IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS occurrence_history (
 id bigserial PRIMARY KEY, occurrence_id uuid NOT NULL REFERENCES occurrence(id),
 actor_id uuid NOT NULL REFERENCES principal(id), operation_id uuid NOT NULL,
 action text NOT NULL CHECK (action IN ('assume','close')), reason text NOT NULL,
 request_hash text NOT NULL, result jsonb NOT NULL,
 occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE (actor_id, operation_id)
);
"""


def seed(conn: psycopg.Connection, case_id: UUID = DEMO_ID) -> None:
    conn.execute(
        "INSERT INTO occurrence(id,shipment,order_ref,reason) VALUES (%s,%s,%s,%s) "
        "ON CONFLICT (id) DO NOTHING",
        (
            case_id,
            "REM-1042",
            "PED-2081",
            "Endereço requer confirmação do destinatário",
        ),
    )


def actor(conn: psycopg.Connection, sid: str, *, lock: bool = False) -> dict:
    suffix = " FOR SHARE OF p" if lock else ""
    row = conn.execute(
        "SELECT p.* FROM principal p JOIN browser_session s ON s.principal_id=p.id "
        "WHERE s.token_hash=%s AND s.expires_at>clock_timestamp()" + suffix,
        (hashlib.sha256(sid.encode()).hexdigest(),),
    ).fetchone()
    if not row:
        raise HTTPException(401, "Entre novamente para continuar.")
    return row


def snapshot(conn: psycopg.Connection, case_id: UUID) -> dict:
    row = conn.execute("SELECT * FROM occurrence WHERE id=%s", (case_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Ocorrência não encontrada.")
    row["history"] = conn.execute(
        "SELECT p.name,h.actor_id,h.action,h.reason,h.occurred_at,h.operation_id "
        "FROM occurrence_history h JOIN principal p ON p.id=h.actor_id "
        "WHERE h.occurrence_id=%s ORDER BY h.id",
        (case_id,),
    ).fetchall()
    return row


class RollbackProbe(Exception):
    """Falha de teste entre atualização da ocorrência e inserção do histórico."""


def change(
    cfg: dict,
    sid: str,
    case_id: UUID,
    action: str,
    version: int,
    operation_id: UUID,
    reason: str,
    *,
    rollback_probe: bool = False,
) -> dict:
    if action not in {"assume", "close"} or version < 0:
        raise HTTPException(422, "Ação ou versão inválida.")
    reason = reason.strip()
    if len(reason) > 500 or (action == "close" and not reason):
        raise HTTPException(422, "Informe uma justificativa de até 500 caracteres.")
    digest = hashlib.sha256(
        json.dumps(
            [str(case_id), action, version, reason],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    # A transação cobre autorização, concorrência, efeito e histórico.
    with connect(cfg) as conn:
        person = actor(conn, sid, lock=True)
        if person["role"] != "operator":
            raise HTTPException(403, "Seu perfil permite apenas consulta.")
        # Serializa a mesma chave também quando usada em ocorrências diferentes.
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (str(person["id"]) + str(operation_id),),
        )
        previous = conn.execute(
            "SELECT request_hash,result FROM occurrence_history "
            "WHERE actor_id=%s AND operation_id=%s",
            (person["id"], operation_id),
        ).fetchone()
        if previous:
            if previous["request_hash"] != digest:
                raise HTTPException(
                    409, "A mesma operação foi enviada com outro conteúdo."
                )
            return {**previous["result"], "replayed": True}
        row = conn.execute(
            "SELECT * FROM occurrence WHERE id=%s FOR UPDATE", (case_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Ocorrência não encontrada.")
        if row["version"] != version:
            raise HTTPException(
                409, "A ocorrência mudou. Atualize a página antes de agir."
            )
        if action == "assume":
            if row["state"] != "OPEN":
                raise HTTPException(409, "A ocorrência já possui responsável.")
            state, owner = "IN_PROGRESS", person["id"]
        else:
            if row["owner_id"] != person["id"]:
                raise HTTPException(403, "Somente o responsável pode encerrar.")
            if row["state"] != "IN_PROGRESS":
                raise HTTPException(409, "A ocorrência não está em atendimento.")
            state, owner = "CLOSED", row["owner_id"]
        conn.execute(
            "UPDATE occurrence SET state=%s,owner_id=%s,version=version+1 WHERE id=%s",
            (state, owner, case_id),
        )
        if rollback_probe:
            raise RollbackProbe()
        result = {
            "id": str(case_id),
            "state": state,
            "version": version + 1,
            "replayed": False,
        }
        conn.execute(
            "INSERT INTO occurrence_history(occurrence_id,actor_id,operation_id,action,reason,"
            "request_hash,result) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                case_id,
                person["id"],
                operation_id,
                action,
                reason,
                digest,
                Jsonb(result),
            ),
        )
    return result


PAGE = """<!doctype html><html lang="pt-BR"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FulfillFlow · Ocorrências</title><style>
body{margin:0;background:#f3f5f7;color:#172b3a;font:16px system-ui,sans-serif}
header{background:#172b3a;color:white;padding:18px 6%;display:flex;justify-content:space-between}
main{max-width:960px;margin:34px auto;padding:0 24px}h1{font-size:30px;margin-bottom:8px}
.muted{color:#586876}.card{background:white;border:1px solid #d7dfe5;border-radius:10px;padding:24px;margin:22px 0}
.pill{background:#dcebea;padding:5px 12px;border-radius:20px;font-size:14px}
dl{display:grid;grid-template-columns:180px 1fr;gap:12px}dd{margin:0}
button,.button{display:inline-block;background:#176761;border:0;border-radius:6px;color:white;padding:11px 18px;font:inherit;cursor:pointer;text-decoration:none}
.secondary{background:#405565}textarea{display:block;width:95%;padding:10px;font:inherit;margin:12px 0;border:1px solid #a3b2bd;border-radius:6px}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{text-align:left;padding:12px 8px;border-bottom:1px solid #e3e7eb}
small{font-size:13px}a{color:#176761}form{margin:0}.error{border-left:5px solid #b53e3e}
</style><header><strong>FulfillFlow</strong><span>Operação de entregas</span></header><main>
{% if error %}<div class="card error"><h1>Ação não concluída</h1><p>{{ error }}</p><a href="/">Voltar às ocorrências</a></div>
{% elif not person %}<h1>Atendimento de ocorrências</h1><p class="muted">Acompanhe pendências e registre as providências da equipe.</p>
<div class="card"><h2>Acesse sua conta</h2><p>Entre para consultar o histórico e continuar um atendimento.</p><a class="button" href="/login">Entrar</a></div>
{% else %}<p class="muted">{{ person.name | title }} · {{ 'Operador' if person.role == 'operator' else 'Consulta' }}</p>
<h1>Confirmação de endereço</h1><p class="muted">{{ item.shipment }} · Pedido {{ item.order_ref }}</p>
<div class="card"><span class="pill">{{ labels[item.state] }}</span><dl>
<dt>Motivo</dt><dd>{{ item.reason }}</dd><dt>Responsável</dt><dd>{{ owner }}</dd>
<dt>Remessa</dt><dd>Em trânsito</dd><dt>Pedido</dt><dd>Em processamento</dd></dl>
{% if person.role == 'operator' and (item.state == 'OPEN' or (item.state == 'IN_PROGRESS' and item.owner_id == person.id)) %}
<form method="post" action="/occurrences/{{ item.id }}/actions">
<input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="version" value="{{ item.version }}">
<input type="hidden" name="operation_id" value="{{ operation_id }}">
{% if item.state == 'OPEN' %}<button name="action" value="assume">Assumir atendimento</button>
{% else %}<label for="reason">Providência e justificativa do encerramento</label>
<textarea id="reason" name="reason" maxlength="500" required rows="3"></textarea>
<button name="action" value="close">Encerrar ocorrência</button>{% endif %}</form>
{% elif item.state != 'CLOSED' %}<p class="muted">{{ 'Seu perfil permite apenas consulta.' if person.role == 'viewer' else 'O atendimento está com outro operador.' }}</p>{% endif %}
<p><small class="muted">O encerramento registra a providência. A situação da remessa e do pedido é acompanhada separadamente.</small></p></div>
<div class="card"><h2>Histórico do atendimento</h2><table><thead><tr><th>Responsável</th><th>Ação</th><th>Registro</th></tr></thead><tbody>
{% for entry in item.history %}<tr><td>{{ entry.name | title }}</td><td>{{ 'Assumiu' if entry.action == 'assume' else 'Encerrou' }}<br><small>{{ entry.reason }}</small></td><td>{{ entry.occurred_at.strftime('%d/%m/%Y %H:%M:%S') }} UTC</td></tr>
{% else %}<tr><td colspan="3">Nenhuma ação registrada.</td></tr>{% endfor %}</tbody></table></div>
<form method="post" action="/logout"><input type="hidden" name="csrf" value="{{ csrf }}"><button class="secondary">Sair</button></form>
{% endif %}</main></html>"""
TEMPLATE = Environment(autoescape=True, undefined=StrictUndefined).from_string(PAGE)


def csrf_check(request: Request, form: dict) -> None:
    token = request.session.get("csrf", "")
    if not token or not secrets.compare_digest(token, str(form.get("csrf", ""))):
        raise HTTPException(403, "Formulário expirado. Atualize a página.")
    if request.headers.get("origin", BASE) != BASE:
        raise HTTPException(403, "Origem do formulário inválida.")


def make_app(cfg: dict) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg["session_key"],
        session_cookie="ff_poc",
        same_site="lax",
        max_age=1800,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1"])
    oauth = OAuth()
    oauth.register(
        "identity",
        client_id="occurrence-poc",
        client_secret=cfg["client_secret"],
        server_metadata_url=ISSUER + "/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile", "code_challenge_method": "S256"},
    )

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "same-origin",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            }
        )
        return response

    @app.exception_handler(HTTPException)
    async def error(request: Request, exc: HTTPException):
        if request.headers.get("accept") == "application/json":
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return HTMLResponse(
            TEMPLATE.render(error=exc.detail), status_code=exc.status_code
        )

    @app.exception_handler(psycopg.Error)
    async def database_error(request: Request, exc: psycopg.Error):
        return await error(
            request,
            HTTPException(
                503, "Dados temporariamente indisponíveis. Tente consultar novamente."
            ),
        )

    @app.get("/health")
    def health():
        with connect(cfg) as conn:
            conn.execute("SELECT 1")
        return {"status": "ok", "pid": os.getpid()}

    @app.get("/login")
    async def login(request: Request):
        request.session.clear()
        return await oauth.identity.authorize_redirect(
            request, BASE + "/callback", prompt="login"
        )

    @app.get("/callback")
    async def callback(request: Request):
        try:
            token = await oauth.identity.authorize_access_token(request)
        except (OAuthError, httpx.HTTPError, ValueError):
            request.session.clear()
            raise HTTPException(401, "Não foi possível confirmar o login.") from None
        identity = token[
            "userinfo"
        ]  # Assinatura, issuer, audience, nonce e prazo pelo Authlib.
        with connect(cfg) as conn:
            person = conn.execute(
                "SELECT id FROM principal WHERE issuer=%s AND subject=%s",
                (identity["iss"], identity["sub"]),
            ).fetchone()
            if not person:
                request.session.clear()
                raise HTTPException(403, "Conta sem acesso à aplicação.")
            sid = secrets.token_urlsafe(32)
            conn.execute(
                "DELETE FROM browser_session WHERE expires_at<clock_timestamp()"
            )
            conn.execute(
                "INSERT INTO browser_session VALUES (%s,%s,clock_timestamp()+interval '30 minutes')",
                (hashlib.sha256(sid.encode()).hexdigest(), person["id"]),
            )
        request.session.clear()
        request.session.update(sid=sid, csrf=secrets.token_urlsafe(32))
        return RedirectResponse("/", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, case: UUID = DEMO_ID):
        if not request.session.get("sid"):
            return TEMPLATE.render(error=None, person=None)
        with connect(cfg) as conn:
            person = actor(conn, request.session["sid"])
            item = snapshot(conn, case)
            owner = conn.execute(
                "SELECT name FROM principal WHERE id=%s", (item["owner_id"],)
            ).fetchone()
        return TEMPLATE.render(
            error=None,
            person=person,
            item=item,
            owner=owner["name"].title() if owner else "Ainda não atribuído",
            csrf=request.session["csrf"],
            operation_id=uuid4(),
            labels={
                "OPEN": "Aberta",
                "IN_PROGRESS": "Em atendimento",
                "CLOSED": "Encerrada",
            },
        )

    @app.get("/api/occurrences/{case_id}")
    def read_case(request: Request, case_id: UUID):
        with connect(cfg) as conn:
            actor(conn, request.session.get("sid", ""))
            return snapshot(conn, case_id)

    @app.post("/occurrences/{case_id}/actions")
    async def action(request: Request, case_id: UUID):
        form = dict(await request.form())
        csrf_check(request, form)
        try:
            version, operation_id = (
                int(str(form.get("version", ""))),
                UUID(str(form.get("operation_id", ""))),
            )
        except ValueError:
            raise HTTPException(422, "Versão ou identificador inválido.") from None
        result = await asyncio.to_thread(
            change,
            cfg,
            request.session.get("sid", ""),
            case_id,
            str(form.get("action", "")),
            version,
            operation_id,
            str(form.get("reason", "")),
        )
        if request.headers.get("accept") == "application/json":
            return result
        return RedirectResponse("/?case=" + str(case_id), status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        csrf_check(request, dict(await request.form()))
        with connect(cfg) as conn:
            conn.execute(
                "DELETE FROM browser_session WHERE token_hash=%s",
                (hashlib.sha256(request.session.get("sid", "").encode()).hexdigest(),),
            )
        request.session.clear()
        return RedirectResponse("/", status_code=303)

    return app


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], text=True, capture_output=True, check=check, timeout=240
    )


def container(cfg: dict, kind: str) -> str:
    return "ff-occurrence-poc-" + cfg["owner"] + "-" + kind


def owned(cfg: dict, kind: str) -> dict | None:
    result = docker("inspect", container(cfg, kind), check=False)
    if result.returncode:
        if "no such" in result.stderr.lower():
            return None
        raise RuntimeError("Docker indisponível: " + result.stderr.strip())
    item = json.loads(result.stdout)[0]
    if item["Config"].get("Labels", {}).get(LABEL) != cfg["owner"]:
        raise RuntimeError("Container não pertence a esta POC.")
    return item


def up() -> None:
    docker("info")
    STATE.mkdir(exist_ok=True)
    path = STATE / "config.json"
    if not path.exists():
        cfg = {
            key: secrets.token_urlsafe(32)
            for key in ("db_password", "session_key", "client_secret", "user_password")
        }
        cfg["owner"] = secrets.token_hex(4)
        path.write_text(json.dumps(cfg), encoding="utf-8")
        path.chmod(0o600)
    cfg = config()
    realm = {
        "realm": "fulfillflow-poc",
        "enabled": True,
        "sslRequired": "none",
        "registrationAllowed": False,
        "resetPasswordAllowed": False,
        "clients": [
            {
                "clientId": "occurrence-poc",
                "secret": cfg["client_secret"],
                "publicClient": False,
                "standardFlowEnabled": True,
                "directAccessGrantsEnabled": False,
                "redirectUris": [BASE + "/callback"],
                "webOrigins": [BASE],
                "attributes": {"pkce.code.challenge.method": "S256"},
            }
        ],
        "users": [
            {
                "id": str(user_id(name)),
                "username": name,
                "enabled": True,
                "firstName": name.title(),
                "lastName": "Sintético",
                "email": name + "@example.invalid",
                "emailVerified": True,
                "credentials": [
                    {
                        "type": "password",
                        "value": cfg["user_password"],
                        "temporary": False,
                    }
                ],
            }
            for name in [*USERS, "sem-acesso"]
        ],
    }
    realm_file = STATE / "fulfillflow-poc-realm.json"
    realm_file.write_text(json.dumps(realm), encoding="utf-8")
    realm_file.chmod(0o600)
    for kind, image in IMAGES.items():
        item = owned(cfg, kind)
        if item:
            if not item["State"]["Running"]:
                docker("start", container(cfg, kind))
            continue
        args = [
            "run",
            "-d",
            "--name",
            container(cfg, kind),
            "--label",
            LABEL + "=" + cfg["owner"],
        ]
        if kind == "db":
            envfile = STATE / "postgres.env"
            envfile.write_text(
                "POSTGRES_DB=occurrence_poc\nPOSTGRES_USER=poc\nPOSTGRES_PASSWORD="
                + cfg["db_password"],
                encoding="utf-8",
            )
            envfile.chmod(0o600)
            args += [
                "-p",
                "127.0.0.1:8742:5432",
                "--env-file",
                str(envfile),
                image,
            ]
        else:
            args += [
                "-p",
                "127.0.0.1:8741:8080",
                "--mount",
                f"type=bind,source={realm_file},target=/opt/keycloak/data/import/fulfillflow-poc-realm.json,readonly",
                image,
                "start-dev",
                "--import-realm",
                "--hostname=" + "http://127.0.0.1:8741",
            ]
        docker(*args)
    deadline = time.monotonic() + 180
    while True:
        try:
            with connect(cfg) as conn:
                conn.execute(SCHEMA)
                for name, role in USERS.items():
                    conn.execute(
                        "INSERT INTO principal VALUES (%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                        (user_id(name), ISSUER, str(user_id(name)), name, role),
                    )
                seed(conn)
            response = httpx.get(
                ISSUER + "/.well-known/openid-configuration", timeout=3
            )
            response.raise_for_status()
            if response.json()["issuer"] != ISSUER:
                raise RuntimeError("Issuer diferente do configurado.")
            break
        except (psycopg.OperationalError, httpx.HTTPError):
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Dependências não ficaram prontas em 180 segundos."
                ) from None
            time.sleep(2)
    print(
        f"Dependências prontas. Estado local: {STATE}\nPróximos comandos: verify, credentials, serve."
    )


def set_role(cfg: dict, name: str, role: str) -> None:
    with connect(cfg) as conn:
        row = conn.execute(
            "UPDATE principal SET role=%s WHERE id=%s RETURNING id",
            (role, user_id(name)),
        ).fetchone()
        if not row:
            raise ValueError("Usuário sintético desconhecido.")


def login_client(cfg: dict, name: str, *, expected: int = 200) -> httpx.Client:
    # Browsers consideram loopback confiável para cookies Secure. HTTPX não.
    # Esta política é exclusiva do teste e nunca permite HTTP fora de loopback.
    class LoopbackCookiePolicy(DefaultCookiePolicy):
        def return_ok_secure(self, cookie, request):
            if request.host in {"127.0.0.1:8740", "127.0.0.1:8741"}:
                return True
            return super().return_ok_secure(cookie, request)

    def local_only(request: httpx.Request) -> None:
        if str(request.url.copy_with(path="/", query=None, fragment=None)) not in {
            BASE + "/",
            "http://127.0.0.1:8741/",
        }:
            raise RuntimeError("Cliente de teste restrito aos dois endereços locais.")
        # HTTPX copia o jar ao preparar requests; reaplica a política e os paths.
        request.headers.pop("cookie", None)
        client.cookies.set_cookie_header(request)

    client = httpx.Client(
        base_url=BASE,
        timeout=15,
        follow_redirects=True,
        cookies=CookieJar(policy=LoopbackCookiePolicy()),
        event_hooks={"request": [local_only]},
    )
    login = client.get("/login")
    login.raise_for_status()
    form = BeautifulSoup(login.text, "html.parser").find("form", id="kc-form-login")
    if form is None:
        client.close()
        raise AssertionError("Formulário real do Keycloak não encontrado.")
    response = client.post(
        form["action"], data={"username": name, "password": cfg["user_password"]}
    )
    if response.status_code != expected or (
        expected == 200 and response.url.host != "127.0.0.1"
    ):
        client.close()
        raise AssertionError(f"Login {name}: status inesperado {response.status_code}.")
    if expected == 200:
        check = client.get("/api/occurrences/" + str(DEMO_ID))
        assert check.status_code == 200, "OIDC não estabeleceu uma sessão local."
    return client


def csrf_token(client: httpx.Client, case_id: UUID = DEMO_ID) -> str:
    page = client.get("/", params={"case": str(case_id)})
    page.raise_for_status()
    return BeautifulSoup(page.text, "html.parser").find(
        "input", attrs={"name": "csrf"}
    )["value"]


def launch_server(handle) -> subprocess.Popen:
    env = os.environ.copy()
    executable = sys.executable
    if os.name == "nt":
        # Evita encerrar apenas o redirecionador do venv no Windows.
        executable = sys._base_executable
        env["__PYVENV_LAUNCHER__"] = sys.executable
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.Popen(
        [executable, str(Path(__file__).resolve()), "serve"],
        env=env,
        stdout=handle,
        stderr=handle,
    )


def verify(cfg: dict, output: Path | None) -> None:
    """Integração HTTP real e teste SQL focal; fixtures novas, sem limpar históricos."""
    results = []
    log = STATE / ("verify-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + ".log")
    case_ids = [uuid4() for _ in range(4)]
    with connect(cfg) as conn:
        for case_id in case_ids:
            seed(conn, case_id)
    clients: list[httpx.Client] = []
    process = None
    started = datetime.now(UTC).isoformat()

    def record(name: str, check: bool, observed: str) -> None:
        results.append(
            {"case": name, "status": "PASS" if check else "FAIL", "observed": observed}
        )
        if not check:
            raise AssertionError(name + ": " + observed)

    def post(
        client,
        case_id,
        action="assume",
        version=0,
        operation_id=None,
        reason="",
        csrf=None,
    ):
        return client.post(
            f"/occurrences/{case_id}/actions",
            headers={"Accept": "application/json", "Origin": BASE},
            data={
                "action": action,
                "version": version,
                "operation_id": str(operation_id or uuid4()),
                "reason": reason,
                "csrf": csrf if csrf is not None else csrf_token(client, case_id),
            },
        )

    completed = False
    try:
        # Um servidor já existente impediria atribuir os resultados a este processo.
        import socket

        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", 8740)) == 0:
                raise RuntimeError(
                    "Encerre o servidor da POC antes de verify (porta 8740 ocupada)."
                )
        with log.open("w", encoding="utf-8") as handle:
            process = launch_server(handle)
            deadline = time.monotonic() + 30
            while True:
                try:
                    ready = httpx.get(BASE + "/health", timeout=2)
                    if ready.status_code == 200 and ready.json()["pid"] == process.pid:
                        break
                except httpx.HTTPError:
                    pass
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Aplicação não iniciou; veja " + str(log))
                time.sleep(0.3)
            for name in USERS:
                clients.append(login_client(cfg, name))
            ana, bruno, viewer = clients
            record(
                "oidc_real",
                True,
                "Três sessões via Authorization Code/PKCE no Keycloak; sem password grant.",
            )
            denied = login_client(cfg, "sem-acesso", expected=403)
            denied.close()
            record(
                "identity_without_grant",
                True,
                "Identidade autenticada no IdP recusada pela aplicação (403).",
            )
            with httpx.Client(base_url=BASE) as anonymous:
                record(
                    "anonymous_read",
                    anonymous.get(f"/api/occurrences/{case_ids[0]}").status_code == 401,
                    "Consulta sem sessão: 401.",
                )
                record(
                    "invalid_oidc_state",
                    anonymous.get("/callback?code=fake&state=fake").status_code == 401,
                    "Callback sem state correlacionado: 401.",
                )
            record(
                "viewer_write",
                post(viewer, case_ids[0]).status_code == 403,
                "Leitor: alteração recusada no servidor.",
            )
            record(
                "csrf",
                post(ana, case_ids[0], csrf="invalid").status_code == 403,
                "POST sem CSRF válido recusado.",
            )
            op = uuid4()
            record(
                "assume",
                post(ana, case_ids[0], operation_id=op).status_code == 200,
                "Ocorrência assumida por Ana.",
            )
            repeat = post(ana, case_ids[0], operation_id=op)
            record(
                "idempotency",
                repeat.status_code == 200 and repeat.json()["replayed"],
                "Mesma intenção retornou o resultado original.",
            )
            record(
                "idempotency_conflict",
                post(ana, case_ids[0], operation_id=op, reason="outro").status_code
                == 409,
                "Chave reaproveitada com conteúdo diferente: 409.",
            )
            record(
                "owner_permission",
                post(bruno, case_ids[0], "close", 1, reason="Resolvido").status_code
                == 403,
                "Outro operador não pode encerrar o atendimento de Ana.",
            )
            record(
                "close_reason",
                post(ana, case_ids[0], "close", 1).status_code == 422,
                "Encerramento exige justificativa.",
            )
            record(
                "close",
                post(
                    ana, case_ids[0], "close", 1, reason="Endereço confirmado"
                ).status_code
                == 200,
                "Encerramento com justificativa persistido.",
            )
            with connect(cfg) as conn:
                row = snapshot(conn, case_ids[0])
            record(
                "atomic_history_and_scope",
                row["state"] == "CLOSED"
                and len(row["history"]) == 2
                and all(x["actor_id"] == user_id("ana") for x in row["history"])
                and row["shipment_status"] == "IN_TRANSIT"
                and row["order_status"] == "PROCESSING",
                "Dois registros com autor; repetição não duplicou histórico; pedido/remessa inalterados.",
            )
            tokens = [csrf_token(c, case_ids[1]) for c in (ana, bruno)]
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(post, c, case_ids[1], csrf=t)
                    for c, t in zip((ana, bruno), tokens)
                ]
                statuses = sorted(f.result().status_code for f in futures)
            with connect(cfg) as conn:
                row = snapshot(conn, case_ids[1])
            record(
                "concurrent_assume",
                statuses == [200, 409] and len(row["history"]) == 1,
                f"Duas sessões/conexões: HTTP {statuses}; um responsável e um registro.",
            )
            set_role(cfg, "ana", "viewer")
            record(
                "revocation_live_session",
                post(ana, case_ids[2]).status_code == 403,
                "Mesmo cookie após commit da revogação: nova operação recusada.",
            )
            set_role(cfg, "ana", "operator")
            # Usa a mesma operação de negócio, com injeção interna inacessível por HTTP.
            with connect(cfg) as conn:
                sid = secrets.token_urlsafe(32)
                conn.execute(
                    "INSERT INTO browser_session VALUES (%s,%s,clock_timestamp()+interval '1 minute')",
                    (hashlib.sha256(sid.encode()).hexdigest(), user_id("ana")),
                )
            try:
                change(
                    cfg, sid, case_ids[3], "assume", 0, uuid4(), "", rollback_probe=True
                )
            except RollbackProbe:
                pass
            else:
                raise AssertionError("Falha de rollback não foi acionada.")
            with connect(cfg) as conn:
                row = snapshot(conn, case_ids[3])
            record(
                "rollback_before_history",
                row["state"] == "OPEN" and row["version"] == 0 and not row["history"],
                "Exceção após UPDATE e antes do histórico: conexão independente observou rollback integral.",
            )
            saved_cookie = dict(ana.cookies)
            response = ana.post("/logout", data={"csrf": csrf_token(ana)})
            with httpx.Client(base_url=BASE, cookies=saved_cookie) as replay:
                record(
                    "logout_revokes_session",
                    response.status_code == 200
                    and replay.get(f"/api/occurrences/{case_ids[0]}").status_code
                    == 401,
                    "Cookie anterior ao logout não recupera sessão removida do banco.",
                )
            process.terminate()
            process.wait(timeout=15)
            process = launch_server(handle)
            deadline = time.monotonic() + 30
            while True:
                try:
                    response = bruno.get(f"/api/occurrences/{case_ids[0]}")
                    ready = bruno.get("/health")
                    if (
                        response.status_code == 200
                        and ready.json()["pid"] == process.pid
                    ):
                        break
                except httpx.HTTPError:
                    pass
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Reinício da aplicação falhou.")
                time.sleep(0.3)
            record(
                "process_restart",
                response.json()["state"] == "CLOSED"
                and len(response.json()["history"]) == 2,
                "Novo processo manteve sessão de Bruno, ocorrência e histórico; não é kill durante transação.",
            )
            completed = True
    finally:
        for client in clients:
            client.close()
        if process and process.poll() is None:
            process.terminate()
            process.wait(timeout=15)
        set_role(cfg, "ana", "operator")
        report = {
            "status": "PASS" if completed else "INCOMPLETE",
            "started_at": started,
            "finished_at": datetime.now(UTC).isoformat(),
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "integration_reference": REFERENCE,
            "results": results,
            "real": [
                "Keycloak OIDC",
                "PostgreSQL",
                "HTTP/TCP",
                "sessões de dois operadores",
            ],
            "injected": ["Exceção após UPDATE antes do histórico"],
            "http_client_note": "O cliente automatizado aceita cookies Secure somente nos dois endereços loopback, como o navegador local. Não configura TLS nem avalia HTTPS de produção.",
            "synthetic": ["identidades", "remessas", "pedidos", "ocorrências"],
            "not_evaluated": [
                "notificação externa",
                "integração com serviços 1.x",
                "réplicas",
                "carga",
                "bloqueio no IdP com sessão local ativa",
            ],
            "dependencies": {
                name: importlib.metadata.version(name)
                for name in ("fastapi", "authlib", "psycopg", "uvicorn", "httpx")
            },
            "cases": list(map(str, case_ids)),
        }
        report["images"] = {
            kind: {
                "image_id": owned(cfg, kind)["Image"],
                "digests": json.loads(docker("image", "inspect", IMAGES[kind]).stdout)[
                    0
                ]["RepoDigests"],
            }
            for kind in IMAGES
        }
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("x", encoding="utf-8") as handle:
                json.dump(report, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
        print(
            json.dumps(
                {"checks": len(results), "results": results},
                ensure_ascii=False,
                indent=2,
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "command", choices=["up", "serve", "verify", "credentials", "role", "stop"]
    )
    parser.add_argument("name", nargs="?", choices=list(USERS))
    parser.add_argument("role", nargs="?", choices=["operator", "viewer"])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "up":
        up()
        return
    cfg = config()
    if args.command == "serve":
        uvicorn.run(
            make_app(cfg),
            host="127.0.0.1",
            port=8740,
            access_log=False,
            log_level="warning",
        )
    elif args.command == "verify":
        if args.output and args.output.exists():
            parser.error(
                "O resultado já existe. Use outro nome para preservar a execução anterior."
            )
        verify(cfg, args.output)
    elif args.command == "credentials":
        print(
            "Usuários sintéticos: "
            + ", ".join(USERS)
            + "\nSenha local: "
            + cfg["user_password"]
        )
    elif args.command == "role":
        if not args.name or not args.role:
            parser.error("role exige usuário e perfil")
        set_role(cfg, args.name, args.role)
        print("Perfil atualizado; novas operações consultarão o valor confirmado.")
    elif args.command == "stop":
        for kind in IMAGES:
            item = owned(cfg, kind)
            if item and item["State"]["Running"]:
                docker("stop", "--time", "20", container(cfg, kind))
        print("Dependências da POC paradas; dados preservados.")


if __name__ == "__main__":
    main()
