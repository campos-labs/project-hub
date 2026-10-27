"""#104: real PostgreSQL/Redis queues, SIGKILL workers and synthetic graph/effect.

Python 3.12 + Docker: python spike_durable_execution.py run
Only the isolated experiment uses the dependencies/images pinned in data/.
Reports, worker logs and synthetic packages remain in .spike/<run>/ (untracked).
Agent Server is documentary triage; this does not benchmark LLMs or DOCX quality.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import functools
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypedDict

SCRIPT = Path(__file__).resolve()
CONFIG = json.loads((SCRIPT.parent / 'data/durable_execution.json').read_text())
SETTINGS = CONFIG['settings']
ROOT = Path(os.environ.get('SPIKE_OUTPUT', '/artifacts'))
DSN = os.environ.get('SPIKE_PG', '')
REDIS = os.environ.get('SPIKE_REDIS', '')
PROCESS_EVENTS = []
if os.environ.get('SPIKE_CONTAINER'):
    import procrastinate
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
    from procrastinate.jobs import Status
    from redis.asyncio import Redis
    from taskiq import AckableMessage, Context, TaskiqDepends
    from taskiq.acks import AcknowledgeType
    from taskiq.receiver import Receiver
    from taskiq_redis import RedisStreamBroker
    from langgraph.graph import StateGraph, START, END
    from langgraph.types import Command, interrupt
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
else:
    RedisStreamBroker = object

# Queue adapters are intentionally small; domain/checkpoint/effect logic is shared.
class TransientFailure(Exception):
    """Use only for the deliberately retryable failure in the shared experiment."""

class PgQueue:

    def __init__(self, dsn, queue, execute, retries=2, heartbeat=0.25, stale_after=1.5,on_exhausted=None):
        if retries < 0 or not 0 < heartbeat < stale_after:
            raise ValueError('Invalid retry/heartbeat settings')
        self.dsn, self.queue = (dsn, queue)
        self.retries, self.heartbeat, self.stale_after = (retries, heartbeat, stale_after)
        self.on_exhausted=on_exhausted
        self.app = procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=dsn, min_size=1, max_size=5, kwargs={'application_name': 'spike104-pg-queue'}))

        @self.app.task(name='spike104.execute', queue=queue, pass_context=True, retry=procrastinate.RetryStrategy(max_attempts=retries, wait=1, retry_exceptions={TransientFailure}) if retries else False)
        async def task(context, payload):
            await execute(payload, context.job.id, context.job.attempts + 1)
        self.task = task

    async def setup(self):
        async with self.app.open_async():
            await self.app.schema_manager.apply_schema_async()

    async def enqueue(self, payload, key, connection=None):
        """Caller opens app; an external connection retains caller's commit/rollback.

        For atomic approval: inside conn.transaction(), INSERT decision, then call
        this with that SAME psycopg.AsyncConnection. No commit occurs here.
        lock is Procrastinate's persistent task coordination key, not a long-lived
        transaction on the application's document/decision tables.
        """
        return await self.task.configure(connection=connection, lock=key).defer_async(payload=payload)

    async def recover_forever(self):
        async with await psycopg.AsyncConnection.connect(self.dsn, autocommit=True, application_name='spike104-pg-reaper') as conn:
            while True:
                cursor = await conn.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0))', ('spike104-reaper:' + self.queue,))
                if (await cursor.fetchone())[0]:
                    try:
                        stalled = await self.app.job_manager.get_stalled_jobs(queue=self.queue, seconds_since_heartbeat=self.stale_after)
                        for job in stalled:
                            if job.attempts >= self.retries:
                                status=await self.on_exhausted(job.task_kwargs['payload'])
                                await self.app.job_manager.finish_job(job, status, delete_job=False)
                            else:
                                await self.app.job_manager.retry_job(job)
                    finally:
                        await conn.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))', ('spike104-reaper:' + self.queue,))
                await asyncio.sleep(self.heartbeat)

    async def worker(self, recover=True):
        async with self.app.open_async():
            reaper = asyncio.create_task(self.recover_forever()) if recover else None
            worker = asyncio.create_task(self.app.run_worker_async(queues=[self.queue], concurrency=1, fetch_job_polling_interval=self.heartbeat, update_heartbeat_interval=self.heartbeat, stalled_worker_timeout=self.stale_after, install_signal_handlers=False, delete_jobs='never'))
            try:
                watched = [worker] + ([reaper] if reaper else [])
                done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    await task
            finally:
                worker.cancel()
                if reaper:
                    reaper.cancel()
                for task in [worker, reaper]:
                    if task:
                        with suppress(asyncio.CancelledError):
                            await task

class IdleClaimBroker(RedisStreamBroker):
    """Explicit extra mechanism to reclaim idle PEL entries without business traffic.

    Unlike the native broker this does not need a separate Redis lock around
    XAUTOCLAIM, which atomically transfers eligible entries. The common domain
    handler must still protect concurrent/repeated effects. For this experiment
    the Receiver admits one delivery at a time, without prefetch buffering.
    """

    async def listen(self):
        cursor = '0-0'
        async with Redis(connection_pool=self.connection_pool) as redis:
            while True:
                batches = await redis.xreadgroup(self.consumer_group_name, self.consumer_name, {self.queue_name: '>'}, block=self.block, count=self.count, noack=False)
                for stream, messages in batches or []:
                    for message_id, fields in messages:
                        yield AckableMessage(data=fields[b'data'], ack=functools.partial(redis.xack, stream, self.consumer_group_name, message_id))
                cursor, pending, *_deleted = await redis.xautoclaim(self.queue_name, self.consumer_group_name, self.consumer_name, min_idle_time=self.idle_timeout, start_id=cursor, count=self.unacknowledged_batch_size)
                for message_id, fields in pending:
                    yield AckableMessage(data=fields[b'data'], ack=functools.partial(redis.xack, self.queue_name, self.consumer_group_name, message_id))

@dataclass
class TaskiqRuntime:
    broker: RedisStreamBroker
    task: Any

    async def submit(self, job_id: str, command_id: str):
        return await self.task.kicker().with_task_id(command_id).kiq(job_id)

    async def serve(self, stop: asyncio.Event):
        self.broker.is_worker_process = True
        receiver = Receiver(self.broker, max_async_tasks=1, max_prefetch=0, ack_type=AcknowledgeType.MANUAL, wait_tasks_timeout=2)
        try:
            await receiver.listen(stop)
        finally:
            await self.broker.shutdown()

def make_taskiq_runtime(redis_url: str, queue: str, handler: Callable[[str], Awaitable[Any]], *, recover_idle: bool=False, before_ack: Callable[[str], Awaitable[None]] | None=None, idle_ms: int=1000):
    broker_type = IdleClaimBroker if recover_idle else RedisStreamBroker
    broker = broker_type(redis_url, queue_name=queue, consumer_group_name='spike104', consumer_name=f'worker-{os.getpid()}-{uuid.uuid4().hex[:8]}', consumer_id='0-0', xread_block=200, xread_count=1, idle_timeout=idle_ms, unacknowledged_batch_size=10, unacknowledged_lock_timeout=2, socket_timeout=3, socket_connect_timeout=2)

    @broker.task(task_name='spike104.execute', ack_type='manual')
    async def execute(job_id: str, context: Context=TaskiqDepends()):
        result = await handler(job_id)
        if before_ack:
            await before_ack(job_id)
        await context.ack()
        return result
    return TaskiqRuntime(broker, execute)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


async def connection():
    return await psycopg.AsyncConnection.connect(DSN, autocommit=True, row_factory=dict_row,
                                               application_name='spike104-domain')


async def sql(query, params=(), *, one=False):
    async with await connection() as conn:
        cursor = await conn.execute(query, params)
        return await cursor.fetchone() if one else await cursor.fetchall() if cursor.description else []


async def event(job_id, stage, **details):
    await sql('INSERT INTO spike.events(job_id,stage,details) VALUES (%s,%s,%s)',
              (job_id, stage, Jsonb(details)))


async def gate(job_id, stage):
    await event(job_id, stage)
    row = await sql('UPDATE spike.jobs SET tripped=true WHERE id=%s AND point=%s AND NOT tripped RETURNING id',
                    (job_id, stage), one=True)
    if row:
        await event(job_id, 'fault_ready', point=stage)
        release=ROOT/f'release-{job_id}-{stage}'
        while not release.exists():
            await asyncio.sleep(1)


async def setup():
    await sql('''CREATE SCHEMA spike;
        CREATE TABLE spike.reviews(id text PRIMARY KEY, doc_hash text NOT NULL, proposal jsonb NOT NULL,
          proposal_hash text NOT NULL, state text NOT NULL DEFAULT 'QUEUED');
        CREATE TABLE spike.jobs(id text PRIMARY KEY, review_id text NOT NULL REFERENCES spike.reviews,
          kind text NOT NULL, backend text NOT NULL, queue text NOT NULL, decision jsonb,
          state text NOT NULL DEFAULT 'QUEUED', attempts int NOT NULL DEFAULT 0,
          point text NOT NULL DEFAULT '', tripped boolean NOT NULL DEFAULT false,
          fail text NOT NULL DEFAULT '', queue_id bigint, error text);
        CREATE TABLE spike.decisions(id text PRIMARY KEY, review_id text UNIQUE REFERENCES spike.reviews,
          body jsonb NOT NULL, transaction_id bigint NOT NULL DEFAULT txid_current());
        CREATE TABLE spike.outbox(id text PRIMARY KEY, job_id text REFERENCES spike.jobs,
          sent boolean NOT NULL DEFAULT false, lease_until timestamptz);
        CREATE TABLE spike.effects(id text PRIMARY KEY, body_hash text NOT NULL, state text NOT NULL);
        CREATE TABLE spike.events(seq bigserial PRIMARY KEY, job_id text, stage text,
          details jsonb NOT NULL, at timestamptz NOT NULL DEFAULT clock_timestamp());''')
    queue = PgQueue(DSN, 'setup', lambda *_: None)
    await queue.setup()
    async with AsyncPostgresSaver.from_conn_string(DSN) as saver:
        await saver.setup()


async def enqueue(conn, job_id, review_id, backend, queue, kind, *, decision=None, point='', fail=''):
    await conn.execute('''INSERT INTO spike.jobs(id,review_id,backend,queue,kind,decision,point,fail)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
                       (job_id, review_id, backend, queue, kind, Jsonb(decision), point, fail))
    if backend=='pg':
        adapter = PgQueue(DSN, queue, lambda *_: None)
        async with adapter.app.open_async():
            queue_id = await adapter.enqueue(job_id, key=review_id, connection=conn)
        await conn.execute('UPDATE spike.jobs SET queue_id=%s WHERE id=%s', (queue_id,job_id))
        proof=await (await conn.execute('SELECT txid_current() AS domain_tx,xmin::text::bigint AS queue_tx FROM procrastinate_jobs WHERE id=%s',(queue_id,))).fetchone()
        assert proof['domain_tx']==proof['queue_tx'], 'Queue insert did not share the domain transaction'
        await conn.execute("INSERT INTO spike.events(job_id,stage,details) VALUES (%s,'shared_transaction',%s)",
                           (job_id,Jsonb(proof)))
    else:
        await conn.execute('INSERT INTO spike.outbox(id,job_id) VALUES (%s,%s)', (job_id,job_id))


async def create_review(backend, queue, *, kind='review', point='', fail=''):
    rid = uuid.uuid4().hex
    proposal = dict(CONFIG['proposal'], document_hash=digest(CONFIG['document']))
    async with await connection() as conn:
        async with conn.transaction():
            await conn.execute('INSERT INTO spike.reviews(id,doc_hash,proposal,proposal_hash) VALUES (%s,%s,%s,%s)',
                               (rid,digest(CONFIG['document']),Jsonb(proposal),digest(proposal)))
            await enqueue(conn,rid,rid,backend,queue,kind,point=point,fail=fail)
    return rid


async def approval_input(rid):
    row=await sql('SELECT doc_hash,proposal_hash FROM spike.reviews WHERE id=%s',(rid,),one=True)
    return dict(expected_doc_hash=row['doc_hash'],expected_proposal_hash=row['proposal_hash'])


async def approve(rid, decision_id, *, expected_doc_hash,expected_proposal_hash,point='', bad='', rollback=False):
    async with await connection() as conn:
        async with conn.transaction():
            review = await (await conn.execute('SELECT * FROM spike.reviews WHERE id=%s FOR UPDATE',(rid,))).fetchone()
            if review['doc_hash']!=expected_doc_hash or review['proposal_hash']!=expected_proposal_hash:
                raise ValueError('Caller approval snapshot is stale')
            body = dict(id=decision_id, doc_hash=review['doc_hash'], proposal=review['proposal'],
                        proposal_hash=review['proposal_hash'], choice='approve', actor='SYNTHETIC HUMAN')
            if bad=='document':
                body['doc_hash']='stale-document'
            if bad=='proposal':
                body['proposal']=dict(body['proposal'],after='R$ 9.999,00')
                body['proposal_hash']=digest(body['proposal'])
            if body['doc_hash']!=review['doc_hash'] or body['proposal_hash']!=review['proposal_hash']:
                raise ValueError('Document/proposal approval conflict')
            previous = await (await conn.execute('SELECT body FROM spike.decisions WHERE review_id=%s',(rid,))).fetchone()
            if previous:
                if previous['body']!=body:
                    raise ValueError('Decision identity conflict')
                return 'replayed'
            if review['state']!='APPROVAL_REQUIRED':
                raise ValueError('Not waiting for approval')
            parent = await (await conn.execute('SELECT backend,queue FROM spike.jobs WHERE id=%s',(rid,))).fetchone()
            await conn.execute('INSERT INTO spike.decisions(id,review_id,body) VALUES (%s,%s,%s)',(decision_id,rid,Jsonb(body)))
            await enqueue(conn,decision_id,rid,parent['backend'],parent['queue'],'resume',decision=body,point=point)
            if rollback:
                raise ValueError('Deliberate rollback after enqueue')
    await gate(decision_id,'after_commit')
    return 'accepted'


async def publish(effect_id, body, job_id):
    expected = canonical(body)
    expected_hash=hashlib.sha256(expected).hexdigest()
    await sql("INSERT INTO spike.effects VALUES (%s,%s,'PREPARED') ON CONFLICT DO NOTHING",(effect_id,expected_hash))
    row=await sql('SELECT * FROM spike.effects WHERE id=%s',(effect_id,),one=True)
    if row['body_hash']!=expected_hash:
        raise ValueError('Effect identity reused with different content')
    path=ROOT/'packages'/f'{effect_id}.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    if row['state']=='COMPLETED' and not path.exists():
        raise ValueError('Completed artifact missing')
    temp=path.with_suffix(f'.{uuid.uuid4().hex}.tmp')
    try:
        with temp.open('xb') as stream:
            stream.write(expected); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temp,path)  # Atomic publication without replacing an existing artifact.
            directory=os.open(path.parent,os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            await event(job_id,'physical_publish',effect_id=effect_id)
        except FileExistsError:
            if path.read_bytes()!=expected:
                raise ValueError('Artifact content conflict')
    finally:
        temp.unlink(missing_ok=True)
    await gate(job_id,'file_published')
    await sql("UPDATE spike.effects SET state='COMPLETED' WHERE id=%s",(effect_id,))
    await gate(job_id,'effect_committed')
    return expected_hash


class ReviewState(TypedDict,total=False):
    review_id: str
    analyzed: bool
    approved: dict
    output_hash: str


async def validate_approval(rid,decision):
    review=await sql('SELECT * FROM spike.reviews WHERE id=%s',(rid,),one=True)
    stored=await sql('SELECT body FROM spike.decisions WHERE review_id=%s',(rid,),one=True)
    if (not stored or stored['body']!=decision or decision['doc_hash']!=review['doc_hash']
            or decision['proposal_hash']!=review['proposal_hash'] or digest(decision['proposal'])!=review['proposal_hash']):
        raise ValueError('Approval no longer matches the document/proposal')


def graph_for(saver, job):
    async def analyze(state):
        await event(job['id'],'analysis_started')
        await gate(job['id'],'model_wait')
        await asyncio.sleep(SETTINGS['simulated_model_seconds'])
        await gate(job['id'],'before_checkpoint')
        return {'analyzed':True}
    async def approval(state):
        await gate(job['id'],'after_checkpoint')
        review=await sql('SELECT * FROM spike.reviews WHERE id=%s',(job['review_id'],),one=True)
        decision=interrupt({'document_hash':review['doc_hash'], 'proposal_hash':review['proposal_hash'],
                            'proposal':review['proposal']})
        await validate_approval(job['review_id'],decision)
        return {'approved':decision}
    async def effect(state):
        await gate(job['id'],'before_effect')
        await validate_approval(job['review_id'],state['approved'])
        body=dict(review_id=job['review_id'],document_hash=state['approved']['doc_hash'],
                  proposal_hash=state['approved']['proposal_hash'],change=state['approved']['proposal'])
        return {'output_hash':await publish(job['review_id'],body,job['id'])}
    graph=StateGraph(ReviewState)
    for name,node in [('analyze',analyze),('approval',approval),('effect',effect)]:
        graph.add_node(name,node)
    graph.add_edge(START,'analyze');graph.add_edge('analyze','approval')
    graph.add_edge('approval','effect');graph.add_edge('effect',END)
    return graph.compile(checkpointer=saver)


async def execute(job_id):
    job=await sql('SELECT * FROM spike.jobs WHERE id=%s',(job_id,),one=True)
    await event(job_id,'delivery',pid=os.getpid())
    # Session lock coordinates the thread; autocommit holds no business transaction.
    async with await connection() as guard:
        await guard.execute('SELECT pg_advisory_lock(hashtextextended(%s,0))',(job['review_id'],))
        try:
            job=await sql('SELECT * FROM spike.jobs WHERE id=%s',(job_id,),one=True)
            if job['state'] in {'DONE','FAILED_FINAL'}:
                if job['state']=='DONE' and job['kind']!='review':
                    eid=job['review_id'] if job['kind']=='resume' else job_id
                    effect=await sql('SELECT * FROM spike.effects WHERE id=%s',(eid,),one=True)
                    path=ROOT/'packages'/f'{eid}.json'
                    if not effect or not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest()!=effect['body_hash']:
                        raise ValueError('Completed effect missing or changed')
                await event(job_id,'duplicate_delivery')
                return
            if job['attempts']>=SETTINGS['retry_limit']+1:
                await sql("UPDATE spike.jobs SET state='FAILED_FINAL',error='Execution attempt ceiling' WHERE id=%s",(job_id,))
                return
            job=await sql("UPDATE spike.jobs SET attempts=attempts+1,state='RUNNING' WHERE id=%s RETURNING *",(job_id,),one=True)
            await event(job_id,'execution',attempt=job['attempts'])
            if job['fail'] and (job['fail']!='transient' or job['attempts']<3):
                if job['fail']!='permanent' and job['attempts']<3:
                    raise TransientFailure('Synthetic transient execution failure')
                await sql("UPDATE spike.jobs SET state='FAILED_FINAL',error=%s WHERE id=%s",('synthetic execution failure',job_id))
                return
            if job['kind']=='package':
                await publish(job_id,{'package':job_id,'synthetic':True},job_id)
            else:
                async with AsyncPostgresSaver.from_conn_string(DSN) as saver:
                    graph=graph_for(saver,job)
                    config={'configurable':{'thread_id':job['review_id']}}
                    snapshot=await graph.aget_state(config)
                    data={'review_id':job['review_id']} if not snapshot.values else None
                    if job['kind']=='resume' and not snapshot.values.get('approved'):
                        data=Command(resume=job['decision'])
                    result=await graph.ainvoke(data,config,durability='sync')
                    state='APPROVAL_REQUIRED' if result.get('__interrupt__') else 'READY'
                    await sql('UPDATE spike.reviews SET state=%s WHERE id=%s',(state,job['review_id']))
            await sql("UPDATE spike.jobs SET state='DONE',error=NULL WHERE id=%s",(job_id,))
        except Exception as exc:
            retry=isinstance(exc,TransientFailure) and job['attempts']<SETTINGS['retry_limit']+1
            await sql('UPDATE spike.jobs SET state=%s,error=%s WHERE id=%s',
                      ('RUNNING' if retry else 'FAILED_FINAL',f'{type(exc).__name__}: {exc}',job_id))
            await event(job_id,'execution_error',retry=retry,error=f'{type(exc).__name__}: {exc}')
            if retry:
                raise
        finally:
            await guard.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))',(job['review_id'],))


async def relay(runtime, queue):
    while True:
        row=await sql('''UPDATE spike.outbox SET lease_until=clock_timestamp()+interval '1 second'
          WHERE id=(SELECT o.id FROM spike.outbox o JOIN spike.jobs j ON j.id=o.job_id
          WHERE NOT o.sent AND j.queue=%s AND (o.lease_until IS NULL OR o.lease_until<clock_timestamp())
          ORDER BY o.id LIMIT 1 FOR UPDATE OF o SKIP LOCKED) RETURNING *''',(queue,),one=True)
        if row:
            await runtime.submit(row['job_id'],row['id'])  # No business transaction around Redis I/O.
            await gate(row['job_id'],'outbox_published')
            await sql('UPDATE spike.outbox SET sent=true WHERE id=%s',(row['id'],))
        else:
            await asyncio.sleep(SETTINGS['heartbeat_seconds'])


async def worker(args):
    async def before_ack(job_id):
        await gate(job_id,'before_ack')
    if args.backend=='pg':
        async def handler(payload,*_):
            await execute(payload)
            await before_ack(payload)
        async def exhausted(jid):
            # Domain terminal state commits BEFORE queue acknowledgement; replay is safe.
            await sql("UPDATE spike.jobs SET state='FAILED_FINAL',error='Crash retry ceiling' WHERE id=%s AND state NOT IN ('DONE','FAILED_FINAL')",(jid,))
            row=await sql('SELECT state FROM spike.jobs WHERE id=%s',(jid,),one=True)
            return Status.SUCCEEDED if row['state']=='DONE' else Status.FAILED
        adapter=PgQueue(DSN,args.queue,handler,heartbeat=SETTINGS['heartbeat_seconds'],stale_after=SETTINGS['stale_seconds'],on_exhausted=exhausted)
        await event(args.queue,'worker_ready',pid=os.getpid(),recover=args.recover)
        await adapter.worker(recover=args.recover)
    else:
        runtime=make_taskiq_runtime(REDIS,args.queue,execute,recover_idle=args.recover,before_ack=before_ack,
                                    idle_ms=int(SETTINGS['stale_seconds']*1000))
        await runtime.broker.startup()
        await event(args.queue,'worker_ready',pid=os.getpid(),recover=args.recover)
        jobs=[asyncio.create_task(relay(runtime,args.queue))]
        if args.command!='relay':
            jobs.append(asyncio.create_task(runtime.serve(asyncio.Event())))
        await asyncio.gather(*jobs)


async def wait_for(predicate, timeout=None):
    deadline=time.monotonic()+(timeout or SETTINGS['case_timeout_seconds'])
    while time.monotonic()<deadline:
        result=await predicate()
        if result:
            return result
        await asyncio.sleep(.05)
    raise TimeoutError('Scenario condition did not complete')


async def job_state(jid,state):
    row=await sql('SELECT * FROM spike.jobs WHERE id=%s',(jid,),one=True)
    if state=='DONE' and row and row['state']=='FAILED_FINAL':
        raise RuntimeError(row['error'])
    return row if row and row['state']==state else None


async def stage(jid,name):
    return await sql('SELECT * FROM spike.events WHERE job_id=%s AND stage=%s ORDER BY seq DESC LIMIT 1',(jid,name),one=True)


async def fault(jid,point,after=0):
    return await sql("SELECT * FROM spike.events WHERE job_id=%s AND stage='fault_ready' AND details->>'point'=%s AND seq>%s ORDER BY seq DESC LIMIT 1",(jid,point,after),one=True)


async def ready(process,queue):
    if process.poll() is not None:
        raise RuntimeError(f'Worker {process.pid} exited {process.returncode}; inspect logs')
    return await sql("SELECT * FROM spike.events WHERE job_id=%s AND stage='worker_ready' AND (details->>'pid')::int=%s",(queue,process.pid),one=True)


async def queue_done(backend,queue):
    if backend=='pg':
        row=await sql("SELECT count(*) AS n FROM procrastinate_jobs WHERE queue_name=%s AND status IN ('todo','doing')",(queue,),one=True)
        return row['n']==0
    async with Redis.from_url(REDIS) as redis:
        pending=await redis.xpending(queue,'spike104')
        groups=await redis.xinfo_groups(queue)
        group=next(group for group in groups if group['name']==b'spike104')
        return (pending['pending']==0 and group['lag']==0
                and await sql('SELECT count(*) AS n FROM spike.outbox o JOIN spike.jobs j ON j.id=o.job_id WHERE j.queue=%s AND NOT o.sent',(queue,),one=True)=={'n':0})


def start_worker(backend,queue,*,recover=True,command='worker'):
    name=f'{queue}-{uuid.uuid4().hex[:5]}'
    log=ROOT/'logs'/f'{name}.log';log.parent.mkdir(parents=True,exist_ok=True)
    args=[sys.executable,str(SCRIPT),command,'--backend',backend,'--queue',queue]
    if recover:
        args.append('--recover')
    with log.open('wb') as stream:
        process=subprocess.Popen(args,stdout=stream,stderr=subprocess.STDOUT)
    PROCESS_EVENTS.append(dict(event='start',pid=process.pid,queue=queue,recover=recover,command=command))
    return process


def stop(process):
    if process and process.poll() is None:
        process.kill();process.wait(timeout=5)  # Linux SIGKILL, not graceful worker shutdown.
        assert process.returncode==-9
        PROCESS_EVENTS.append(dict(event='kill',pid=process.pid,returncode=process.returncode))


async def business_locks():
    row=await sql('''SELECT count(DISTINCT a.pid) AS n FROM pg_stat_activity a JOIN pg_locks l ON l.pid=a.pid
      WHERE a.pid<>pg_backend_pid() AND a.datname=current_database() AND a.xact_start IS NOT NULL
      AND l.relation IN ('spike.reviews'::regclass,'spike.decisions'::regclass,'spike.effects'::regclass)''',one=True)
    return row['n']


async def finish_review(rid):
    await wait_for(lambda:job_state(rid,'DONE'))
    decision=uuid.uuid4().hex
    await approve(rid,decision,**await approval_input(rid))
    await wait_for(lambda:job_state(decision,'DONE'))
    expected=await sql('SELECT * FROM spike.effects WHERE id=%s',(rid,),one=True)
    path=ROOT/'packages'/f'{rid}.json'
    assert expected['state']=='COMPLETED' and hashlib.sha256(path.read_bytes()).hexdigest()==expected['body_hash']
    count=await sql("SELECT count(*) AS n FROM spike.events WHERE stage='physical_publish' AND details->>'effect_id'=%s",(rid,),one=True)
    assert count['n']==1
    return decision


async def redeliver(backend,queue,rid,jid):
    if backend=='pg':
        adapter=PgQueue(DSN,queue,lambda *_:None)
        async with adapter.app.open_async():
            await adapter.enqueue(jid,key=rid)
    else:
        runtime=make_taskiq_runtime(REDIS,queue,execute)
        await runtime.broker.startup()
        try:
            await runtime.submit(jid,uuid.uuid4().hex)
        finally:
            await runtime.broker.shutdown()


async def scenario(backend,case):
    if case=='outbox_publish_gap' and backend=='pg':
        return dict(backend=backend,case=case,status='NOT_APPLICABLE',reason='PG enqueue shares the decision transaction')
    queue=f'{backend}-{case}-{uuid.uuid4().hex[:6]}'
    process=None;children=[];started=time.monotonic();details={}
    try:
        point=case if case in {'before_checkpoint','after_checkpoint'} else 'before_checkpoint' if case=='crash_retry_exhausted' else 'model_wait' if case=='business_locks' else ''
        package=case in {'transient_retry','retry_exhausted','permanent_failure'}
        fail={'transient_retry':'transient','retry_exhausted':'always','permanent_failure':'permanent'}.get(case,'')
        rid=await create_review(backend,queue,kind='package' if package else 'review',point=point,fail=fail)
        if case=='accepted_without_worker':
            assert await job_state(rid,'QUEUED')
        process=start_worker(backend,queue)
        await wait_for(lambda:ready(process,queue))
        if case=='crash_retry_exhausted':
            sequence=0
            for _ in range(3):
                marker=await wait_for(lambda:fault(rid,point,sequence))
                sequence=marker['seq'];stop(process)
                await sql('UPDATE spike.jobs SET tripped=false WHERE id=%s',(rid,))
                process=start_worker(backend,queue)
            row=await wait_for(lambda:job_state(rid,'FAILED_FINAL'))
            assert row['attempts']==3 and not (ROOT/'packages'/f'{rid}.json').exists()
            await wait_for(lambda:queue_done(backend,queue))
            return dict(backend=backend,case=case,status='PASS',attempts=3)
        if case=='business_locks':
            await wait_for(lambda:fault(rid,'model_wait'))
            assert await business_locks()==0
            (ROOT/f'release-{rid}-model_wait').touch()
        elif point:
            await wait_for(lambda:fault(rid,point))
            stop(process)
            restart=time.monotonic();process=start_worker(backend,queue)
            await wait_for(lambda:ready(process,queue))
            details['restart_ready_seconds']=round(time.monotonic()-restart,3)
            await wait_for(lambda:job_state(rid,'DONE'))
            details['recovery_seconds']=round(time.monotonic()-restart,3)
            count=await sql("SELECT count(*) AS n FROM spike.events WHERE job_id=%s AND stage='analysis_started'",(rid,),one=True)
            assert count['n']==(2 if case=='before_checkpoint' else 1)
        if package:
            expected='DONE' if case=='transient_retry' else 'FAILED_FINAL'
            row=await wait_for(lambda:job_state(rid,expected))
            assert row['attempts']==(1 if case=='permanent_failure' else 3)
            details['attempts']=row['attempts']
        else:
            await wait_for(lambda:job_state(rid,'DONE'))
            if case=='human_releases_worker':
                assert await business_locks()==0
                probe=await create_review(backend,queue,kind='package')
                await wait_for(lambda:job_state(probe,'DONE'))
                assert not await sql('SELECT * FROM spike.decisions WHERE review_id=%s',(rid,),one=True)
            if case=='transaction_rollback':
                did=uuid.uuid4().hex
                try:
                    await approve(rid,did,rollback=True,**await approval_input(rid))
                except ValueError:
                    pass
                assert not await sql('SELECT * FROM spike.decisions WHERE id=%s',(did,),one=True)
                assert not await sql('SELECT * FROM spike.jobs WHERE id=%s',(did,),one=True)
                if backend=='pg':
                    row=await sql("SELECT count(*) AS n FROM procrastinate_jobs WHERE args->>'payload'=%s",(did,),one=True)
                    assert row['n']==0
            if case=='invalid_approvals':
                for bad in ['document','proposal']:
                    try:
                        await approve(rid,uuid.uuid4().hex,bad=bad,**await approval_input(rid))
                        raise AssertionError('Incompatible approval accepted')
                    except ValueError:
                        pass
                assert not await sql('SELECT * FROM spike.decisions WHERE review_id=%s',(rid,),one=True)
            if case=='stale_caller_approval':
                captured=await approval_input(rid)
                proposal=dict(CONFIG['proposal'],after='changed after human read',document_hash=digest(CONFIG['document']))
                await sql('UPDATE spike.reviews SET proposal=%s,proposal_hash=%s WHERE id=%s',(Jsonb(proposal),digest(proposal),rid))
                try:
                    await approve(rid,uuid.uuid4().hex,**captured)
                    raise AssertionError('A stale approval authorized a different proposal')
                except ValueError:
                    pass
                assert not await sql('SELECT * FROM spike.decisions WHERE review_id=%s',(rid,),one=True)
                return dict(backend=backend,case=case,status='PASS',rejected=True)
            if case=='stale_effect_checkpoint':
                did=uuid.uuid4().hex
                await approve(rid,did,point='before_effect',**await approval_input(rid))
                await wait_for(lambda:fault(did,'before_effect'))
                stop(process)
                await sql("UPDATE spike.reviews SET doc_hash='new-revision' WHERE id=%s",(rid,))
                process=start_worker(backend,queue)
                await wait_for(lambda:job_state(did,'FAILED_FINAL'))
                await wait_for(lambda:queue_done(backend,queue))
                assert not (ROOT/'packages'/f'{rid}.json').exists()
                return dict(backend=backend,case=case,status='PASS',rejected=True)
            if case in {'stale_queued_resume','tampered_resume'}:
                stop(process);did=uuid.uuid4().hex
                await approve(rid,did,**await approval_input(rid))
                if case=='stale_queued_resume':
                    await sql("UPDATE spike.reviews SET doc_hash='new-revision' WHERE id=%s",(rid,))
                else:
                    await sql("UPDATE spike.jobs SET decision=jsonb_set(decision,'{proposal,after}',to_jsonb('unapproved text'::text)) WHERE id=%s",(did,))
                process=start_worker(backend,queue)
                await wait_for(lambda:job_state(did,'FAILED_FINAL'))
                await wait_for(lambda:queue_done(backend,queue))
                assert not (ROOT/'packages'/f'{rid}.json').exists()
                assert not await sql('SELECT * FROM spike.effects WHERE id=%s',(rid,),one=True)
                return dict(backend=backend,case=case,status='PASS',rejected=True)
            if case in {'artifact_missing','artifact_conflict'}:
                did=await finish_review(rid)
                await wait_for(lambda:queue_done(backend,queue))
                path=ROOT/'packages'/f'{rid}.json'
                if case=='artifact_missing':
                    path.unlink()
                else:
                    path.write_bytes(b'synthetic conflicting artifact')
                await redeliver(backend,queue,rid,did)
                await wait_for(lambda:job_state(did,'FAILED_FINAL'))
                await wait_for(lambda:queue_done(backend,queue))
                assert not path.exists() if case=='artifact_missing' else path.read_bytes()==b'synthetic conflicting artifact'
                return dict(backend=backend,case=case,status='PASS',rejected=True)
            if case in {'file_published','effect_committed','before_ack'}:
                did=uuid.uuid4().hex
                await approve(rid,did,point=case,**await approval_input(rid))
                await wait_for(lambda:fault(did,case))
                path=ROOT/'packages'/f'{rid}.json';before=(path.read_bytes(),path.stat().st_mtime_ns)
                stop(process);restart=time.monotonic();process=start_worker(backend,queue)
                await wait_for(lambda:job_state(did,'DONE'))
                await wait_for(lambda:queue_done(backend,queue))
                assert (path.read_bytes(),path.stat().st_mtime_ns)==before
                details['recovery_seconds']=round(time.monotonic()-restart,3)
            elif case in {'commit_before_dispatch','outbox_publish_gap'}:
                stop(process);did=uuid.uuid4().hex
                captured=await approval_input(rid)
                args=[sys.executable,str(SCRIPT),'approve','--review',rid,'--decision',did,'--point','after_commit',
                      '--doc-hash',captured['expected_doc_hash'],'--proposal-hash',captured['expected_proposal_hash']]
                child=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);children.append(child)
                await wait_for(lambda:fault(did,'after_commit'))
                stop(child)
                if case=='outbox_publish_gap':
                    await sql("UPDATE spike.jobs SET point='outbox_published',tripped=false WHERE id=%s",(did,))
                    child=start_worker(backend,queue,command='relay');children.append(child)
                    await wait_for(lambda:fault(did,'outbox_published'))
                    stop(child)
                restart=time.monotonic();process=start_worker(backend,queue)
                await wait_for(lambda:job_state(did,'DONE'))
                if case=='outbox_publish_gap':
                    await wait_for(lambda:stage(did,'duplicate_delivery'))
                details['recovery_seconds']=round(time.monotonic()-restart,3)
            elif case=='concurrent_deliveries':
                second=start_worker(backend,queue);children.append(second)
                await wait_for(lambda:ready(second,queue))
                did=uuid.uuid4().hex
                await approve(rid,did,point='before_effect',**await approval_input(rid))
                await wait_for(lambda:fault(did,'before_effect'))
                await redeliver(backend,queue,rid,did)
                if backend=='redis':
                    async def two_deliveries():
                        row=await sql("SELECT count(DISTINCT details->>'pid') AS n FROM spike.events WHERE job_id=%s AND stage='delivery'",(did,),one=True)
                        return row['n']==2
                    await wait_for(two_deliveries)
                else:
                    row=await sql("SELECT count(*) AS n FROM procrastinate_jobs WHERE args->>'payload'=%s AND status='todo'",(did,),one=True)
                    assert row['n']==1
                (ROOT/f'release-{did}-before_effect').touch()
                await wait_for(lambda:job_state(did,'DONE'))
                await wait_for(lambda:queue_done(backend,queue))
                assert await stage(did,'duplicate_delivery')
                details['workers']=2;details['deliveries']=2
            elif case=='concurrent_resume':
                children.append(start_worker(backend,queue))
                did=uuid.uuid4().hex
                captured=await approval_input(rid)
                publishers=[subprocess.Popen([sys.executable,str(SCRIPT),'approve','--review',rid,'--decision',did,
                            '--doc-hash',captured['expected_doc_hash'],'--proposal-hash',captured['expected_proposal_hash']],
                            stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(2)]
                children.extend(publishers)
                for child in publishers:
                    _,error=await asyncio.to_thread(child.communicate,timeout=10)
                    assert child.returncode==0,error.decode()
                await wait_for(lambda:job_state(did,'DONE'))
                decisions=await sql('SELECT count(*) AS n FROM spike.decisions WHERE review_id=%s',(rid,),one=True)
                assert decisions['n']==1
                details['workers']=2;details['publishers']=2
            else:
                did=await finish_review(rid)
            if backend=='pg':
                row=await sql("SELECT e.details FROM spike.events e JOIN spike.jobs j ON j.id=e.job_id WHERE j.review_id=%s AND j.kind='resume' AND e.stage='shared_transaction'",(rid,),one=True)
                assert row['details']['domain_tx']==row['details']['queue_tx']
                details['shared_transaction']=True
            effect=await sql('SELECT * FROM spike.effects WHERE id=%s',(rid,),one=True)
            path=ROOT/'packages'/f'{rid}.json'
            assert effect['state']=='COMPLETED' and hashlib.sha256(path.read_bytes()).hexdigest()==effect['body_hash']
            count=await sql("SELECT count(*) AS n FROM spike.events WHERE stage='physical_publish' AND details->>'effect_id'=%s",(rid,),one=True)
            assert count['n']==1
        await wait_for(lambda:queue_done(backend,queue))
        return dict(backend=backend,case=case,status='PASS',seconds=round(time.monotonic()-started,3),**details)
    finally:
        stop(process)
        for child in children:
            stop(child)


async def baseline(backend):
    queue=f'{backend}-baseline-{uuid.uuid4().hex[:6]}'
    rid=await create_review(backend,queue,point='after_checkpoint')
    process=start_worker(backend,queue,recover=False)
    try:
        await wait_for(lambda:stage(rid,'fault_ready'))
        stop(process);process=start_worker(backend,queue,recover=False)
        await wait_for(lambda:ready(process,queue))
        await asyncio.sleep(SETTINGS['baseline_observation_seconds'])
        recovered=bool(await job_state(rid,'DONE'))
        stop(process);started=time.monotonic();process=start_worker(backend,queue)
        await wait_for(lambda:ready(process,queue))
        restart_ready=round(time.monotonic()-started,3)
        await wait_for(lambda:job_state(rid,'DONE'))
        recovered_seconds=round(time.monotonic()-started,3)
        await finish_review(rid)
        await wait_for(lambda:queue_done(backend,queue))
        return dict(backend=backend,case='native_without_recovery',status='OBSERVATION',
                    recovered_without_helper=recovered,observation_seconds=SETTINGS['baseline_observation_seconds'],
                    restart_ready_seconds=restart_ready,configured_recovery_seconds=recovered_seconds)
    finally:
        stop(process)


async def experiment(args):
    ROOT.mkdir(parents=True,exist_ok=True)
    await setup()
    manifest=dict(config=CONFIG,script_sha256=hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),
                  python=sys.version,dependencies=subprocess.check_output([sys.executable,'-m','pip','freeze'],text=True).splitlines(),
                  postgres=(await sql('SELECT version() AS version',one=True))['version'],
                  simulated_model=True,real_queues=True)
    results=[]
    async with Redis.from_url(REDIS) as redis:
        manifest['redis']=await redis.info('server')
        manifest['redis_persistence']=await redis.config_get('append*')
    for backend in ['pg','redis']:
        try:
            row=await baseline(backend);results.append(row);print(json.dumps(row),flush=True)
        except Exception as exc:
            row=dict(backend=backend,case='baseline',status='FAILED',error=repr(exc));results.append(row);print(json.dumps(row),flush=True)
        for case in args.case or CONFIG['cases']:
            try:
                row=await scenario(backend,case)
            except Exception as exc:
                row=dict(backend=backend,case=case,status='FAILED',error=repr(exc))
            results.append(row);print(json.dumps(row),flush=True)
    kills=[event for event in PROCESS_EVENTS if event['event']=='kill']
    manifest['real_sigkill']=bool(kills) and all(event['returncode']==-9 for event in kills)
    report=dict(manifest=manifest,results=results,selected_cases=args.case or CONFIG['cases'],
                pending=[case for case in CONFIG['cases'] if args.case and case not in args.case],
                passed=sum(row['status']=='PASS' for row in results),
                failed=sum(row['status']=='FAILED' for row in results),
                events=await sql('SELECT * FROM spike.events ORDER BY seq'),
                processes=PROCESS_EVENTS,jobs=await sql('SELECT * FROM spike.jobs ORDER BY id'),
                effects=await sql('SELECT * FROM spike.effects ORDER BY id'))
    (ROOT/'result.json').write_text(json.dumps(report,indent=2,default=str))
    return 1 if report['failed'] else 0


def docker(*args,**kwargs):
    kwargs.setdefault('check',True)
    return subprocess.run(['docker',*args],text=True,**kwargs)


def bootstrap(args):
    run_id='s104-'+uuid.uuid4().hex[:8]
    output=SCRIPT.parent/'.spike'/run_id;output.mkdir(parents=True)
    shutil.copyfile(SCRIPT,output/'source.py')
    shutil.copyfile(SCRIPT.parent/'data/durable_execution.json',output/'config.json')
    image='campos-spike104:'+hashlib.sha256(SCRIPT.read_bytes()+canonical(CONFIG)).hexdigest()[:12]
    with tempfile.TemporaryDirectory(prefix='build-',dir=output) as context:
        context=Path(context);shutil.copyfile(SCRIPT,context/SCRIPT.name)
        (context/'data').mkdir();shutil.copyfile(SCRIPT.parent/'data/durable_execution.json',context/'data/durable_execution.json')
        dockerfile=f"FROM {CONFIG['images']['python']}\nENV PYTHONUNBUFFERED=1\nRUN pip install --no-cache-dir "+' '.join(CONFIG['dependencies'])+f"\nWORKDIR /app\nCOPY . /app\nENTRYPOINT [\"python\",\"/app/{SCRIPT.name}\"]\n"
        docker('build','-t',image,'-f','-',str(context),input=dockerfile)
    names=dict(network=run_id,pg=run_id+'-pg',redis=run_id+'-redis',runner=run_id+'-runner')
    (output/'resources.json').write_text(json.dumps(dict(names=names,image=image),indent=2))
    docker('network','create','--subnet',args.subnet,names['network'],stdout=subprocess.DEVNULL)
    try:
        docker('run','-d','--name',names['pg'],'--network',run_id,'--network-alias','pg',
               '-e','POSTGRES_PASSWORD=synthetic-only','-e','POSTGRES_DB=spike',
               '-v',run_id+'-pg:/var/lib/postgresql',
               CONFIG['images']['postgres'],stdout=subprocess.DEVNULL)
        docker('run','-d','--name',names['redis'],'--network',run_id,'--network-alias','redis',
               '-v',run_id+'-redis:/data',
               CONFIG['images']['redis'],'redis-server','--appendonly','yes','--appendfsync','always',stdout=subprocess.DEVNULL)
        for _ in range(60):
            ready=subprocess.run(['docker','exec',names['pg'],'pg_isready','-U','postgres','-d','spike'],capture_output=True).returncode==0
            if ready:
                break
            time.sleep(.5)
        else:
            raise TimeoutError('PostgreSQL readiness')
        result=docker('run','--name',names['runner'],'--network',run_id,
                      '-e','SPIKE_CONTAINER=1','-e','SPIKE_PG=postgresql://postgres:synthetic-only@pg/spike',
                      '-e','SPIKE_REDIS=redis://redis:6379','-v',run_id+'-artifacts:/artifacts',image,'experiment',
                      *[value for case in args.case or [] for value in ['--case',case]],check=False)
        return result.returncode
    finally:
        copied=subprocess.run(['docker','cp',names['runner']+':/artifacts/.',str(output)],capture_output=True).returncode==0
        with (output/'database.sql').open('wb') as dump:
            dumped=subprocess.run(['docker','exec',names['pg'],'pg_dump','-U','postgres','-d','spike'],stdout=dump,stderr=subprocess.DEVNULL).returncode==0
        subprocess.run(['docker','exec',names['redis'],'redis-cli','SAVE'],capture_output=True)
        redis_dumped=subprocess.run(['docker','cp',names['redis']+':/data/dump.rdb',str(output/'redis.rdb')],capture_output=True).returncode==0
        for name in [names['runner'],names['redis'],names['pg']]:
            subprocess.run(['docker','rm','-f',name],capture_output=True)
        subprocess.run(['docker','network','rm',run_id],capture_output=True)
        if copied and dumped and redis_dumped:
            for suffix in ['artifacts','pg','redis']:
                subprocess.run(['docker','volume','rm',run_id+'-'+suffix],capture_output=True)
        print(f'Artifacts: {output}',flush=True)


def main():
    logging.basicConfig(level=logging.ERROR)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',nargs='?',default='run',choices=['run','experiment','worker','relay','approve'])
    parser.add_argument('--backend',choices=['pg','redis'])
    parser.add_argument('--queue');parser.add_argument('--recover',action='store_true')
    parser.add_argument('--review');parser.add_argument('--decision');parser.add_argument('--point',default='')
    parser.add_argument('--doc-hash');parser.add_argument('--proposal-hash')
    parser.add_argument('--case',action='append',choices=CONFIG['cases'])
    parser.add_argument('--subnet',default=SETTINGS['docker_subnet'],help='Unused isolated Docker subnet; does not alter global address pools')
    args=parser.parse_args()
    if args.command=='run':
        return bootstrap(args)
    if not os.environ.get('SPIKE_CONTAINER'):
        raise RuntimeError('Internal commands run only inside the isolated experiment container')
    if args.command=='experiment':
        return asyncio.run(experiment(args))
    if args.command=='approve':
        if not args.doc_hash or not args.proposal_hash:
            raise ValueError('Approval requires caller-supplied document/proposal hashes')
        print(asyncio.run(approve(args.review,args.decision,point=args.point,
              expected_doc_hash=args.doc_hash,expected_proposal_hash=args.proposal_hash)),flush=True)
    else:
        asyncio.run(worker(args))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
