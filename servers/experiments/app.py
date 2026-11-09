"""Private experiment library: real requests to Viv, tracked as Rocket.Chat threads."""
import json
from heapq import nlargest
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlsplit

import requests
import yaml
from flask import Flask, Response, abort, jsonify, redirect, request, session, url_for, send_file
from jinja2 import Environment, StrictUndefined

from .runner import RocketChatClient
from .coordinator import CoordinatorError, SimulationCoordinatorClient

ROLES = {'admin':'Administration','hr':'People','finance':'Finance','ds':'Data','pm':'Product','bm':'Business'}
COMPANY_TOOLS = (
    ('erpnext', 'ERPNext'),
    ('payroll', 'Payroll'),
    ('lending', 'Lending'),
    ('crm', 'CRM'),
    ('helpdesk', 'Helpdesk'),
    ('email', 'Email'),
    ('rocketchat', 'Rocket.Chat'),
    ('owncloud', 'ownCloud'),
    ('plane', 'Plane'),
    ('midpoint', 'midPoint'),
)


def _kernel_virtual_time_rate():
    rate_path = Path('/sys/kernel/virtual_time_rate')
    try:
        raw_rate = rate_path.read_text(encoding='ascii').strip()
    except FileNotFoundError:
        return 1
    except OSError as error:
        raise RuntimeError('Could not read the active kernel virtual-time rate') from error
    try:
        rate = int(raw_rate)
    except ValueError as error:
        raise RuntimeError('The active kernel virtual-time rate is invalid') from error
    if rate < 1:
        raise RuntimeError('The active kernel virtual-time rate must be positive')
    return rate


COMPANY_EVALUATION_PROVIDERS = ('coffeebench', 'ecommercebench')
SIMULATION_SCENARIOS = {
    'A1': {
        'title': 'Supplier invoice exceeds goods received',
        'summary': 'A supplier has invoiced the full order, but the warehouse has recorded fewer components. Finance needs the discrepancy investigated before the invoice can progress.',
        'detail': 'A supplier has invoiced 10,000 components against an order for the same quantity, while the receiving records show only 8,000. The company needs to establish why the records disagree and record an accurate invoice review. The outstanding delivery and any payment decision must reflect the evidence and existing approval policy.',
        'benchmarks': [], 'dependencies': ['ERPNext']},
    'B1': {
        'title': 'Late delivery threatens the business schedule',
        'summary': 'An expected shipment has been delayed. The company needs a reliable arrival estimate and clear updates for the teams depending on it.',
        'detail': 'An expected delivery is delayed, leaving the teams that depend on it uncertain about their plans. Follow up on the shipment, establish the revised arrival estimate and keep affected stakeholders informed as the situation changes. Delivery records should reflect what has actually arrived.',
        'benchmarks': [], 'dependencies': ['SimPy', 'Email']},
    'C1': {
        'title': 'Supplier disputes a short delivery',
        'summary': 'The warehouse reports a short delivery, but the supplier says the full order was dispatched. The company needs evidence to resolve the dispute.',
        'detail': 'Company receiving records show 2,000 components missing, while the supplier believes it dispatched the full quantity. The delivery dispute is holding up a clear account of what is still owed. Obtain the supplier’s dispatch evidence and reconcile it with the company’s receiving records without treating either account as proven in advance.',
        'benchmarks': ['CoffeeBench'], 'dependencies': ['Email']},
    'D1': {
        'title': 'Supply disruption prompts a higher quotation',
        'summary': 'A supplier is revising its price after a supply disruption. The company needs to understand the new offer while protecting its existing contract.',
        'detail': 'An existing purchase order is priced at £50 per unit, but a supply disruption has changed the supplier’s position on future pricing. Obtain the revised quotation, assess its implications and keep the original purchase commitment clear. Any proposed contract change needs the appropriate authorization.',
        'benchmarks': ['E-Commerce Bench'], 'dependencies': ['ERPNext', 'Email']},
    'E1': {
        'title': 'Damage complaint lacks supporting details',
        'summary': 'A customer reports damaged components without the serial numbers needed to investigate. The company needs to identify the order and gather the missing evidence.',
        'detail': 'A customer has complained about damaged components but has not supplied serial numbers. Identify the relevant order, clarify what happened and request the missing details. The customer needs an accurate response, and the company needs a clear record of the evidence still outstanding before deciding on a remedy.',
        'benchmarks': ['τ-bench'], 'dependencies': ['ERPNext', 'Email']},
    'S01': {
        'title': 'Delivery dispute blocks an invoice and customer commitment',
        'summary': 'A disputed supplier invoice, missing delivery and customer commitment are connected. The company needs coordinated follow-up across finance, operations and its trading partners.',
        'detail': 'A supplier invoice is blocked by a delivery discrepancy that also affects production and a customer commitment. Finance, warehouse, logistics, the supplier and the customer hold different parts of the picture. Coordinate their evidence and updates as the situation develops, resolve the linked business issues and obtain the required authorization for consequential commitments.',
        'benchmarks': ['CoffeeBench', 'E-Commerce Bench', 'τ-bench'], 'dependencies': ['ERPNext', 'Email', 'SimPy']},
}


def company_tools_urls(path):
    """Read the configured company links shared by navigation and task instructions."""
    mapping = json.loads(Path(path).read_text(encoding='utf-8'))
    expected = {key for key, _ in COMPANY_TOOLS}
    if not isinstance(mapping, dict) or set(mapping) != expected:
        raise ValueError('mapping keys do not match the company tools')
    for key, value in mapping.items():
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(f'invalid URL for {key}')
        parsed = urlsplit(value)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError(f'invalid URL for {key}')
        _ = parsed.port
    return mapping


def catalog(root):
    tools = dict(COMPANY_TOOLS)
    templates = Environment(autoescape=False, undefined=StrictUndefined, keep_trailing_newline=True)
    result = {}
    briefs = {}
    for brief_file in sorted(Path(__file__).parent.glob('briefs-*.json')):
        briefs.update(json.loads(brief_file.read_text()))
    for path in sorted((Path(root)/'workspaces/tasks').glob('*/task.md')):
        task_id = path.parent.name
        if task_id == 'example':
            continue
        description = templates.from_string(path.read_text()).render(company_tools=tools)
        dependencies = yaml.safe_load((path.parent/'dependencies.yml').read_text()) or []
        result[task_id] = dict(id=task_id, title=task_id.removeprefix(task_id.split('-')[0]+'-').replace('-', ' ').capitalize(),
            summary=briefs[task_id]['summary'], description=description,
            display_description=briefs[task_id]['description'],
            role=ROLES.get(task_id.split('-')[0], 'Workplace'), dependencies=dependencies,
            kind='workplace', benchmarks=['The Agent Company'])
    for scenario_id, details in SIMULATION_SCENARIOS.items():
        result[scenario_id] = dict(id=scenario_id, title=details['title'], summary=details['summary'],
            description=details['detail'], display_description=details['detail'], role='Finance',
            dependencies=details['dependencies'], kind='simulation', benchmarks=details['benchmarks'])
    retail = json.loads(Path(__file__).with_name('retail-catalog.json').read_text())
    for entry in retail['tasks']:
        result[entry['id']] = {**entry, 'display_description': entry['description']}
    return result


def public_compatibility():
    """Return the deliberately small, public-safe catalogue summary."""
    path=Path(__file__).with_name('benchmark-compatibility.json')
    source=json.loads(path.read_text())['public_catalogue']
    return {
        'schema_version':source['schema_version'],
        'company_persona':source['company_persona'],
        'library_inventory':[{
            key:item[key] for key in ('name','count','classification','summary')
        } for item in source['library_inventory']],
        'tau_domains':[{
            key:item[key] for key in (
                'name','catalogue_count','default_base_count','fit','summary','implementation',
                'source_integrity_issue_count','financial_task_count','remaining_carrier_state_task_count'
            ) if key in item
        } for item in source['tau_domains']],
        'source_revisions':[{
            key:item[key] for key in ('name','revision')
        } for item in source['source_revisions']],
    }


def create_app(config=None, client_factory=RocketChatClient.from_env,
               coordinator_factory=SimulationCoordinatorClient.from_env):
    app = Flask(__name__)
    app.config.update(SECRET_KEY=os.environ.get('EXPERIMENTS_SECRET_KEY'),
        DATABASE=os.environ.get('EXPERIMENTS_DB', '/data/experiments.sqlite'),
        REPO=os.environ.get('TAC_REPO', str(Path(__file__).resolve().parents[2])),
        COMPANY_PROFILE_FILE=os.environ.get('COMPANY_PROFILE_FILE'),
        CHAT_PUBLIC_URL=os.environ.get('ROCKETCHAT_PUBLIC_URL','https://chat.company.example.test'),
        EXPERIMENTS_PUBLIC_URL=os.environ.get('EXPERIMENTS_PUBLIC_URL'),
        COMPANY_TOOLS_URLS_FILE=os.environ.get(
            'COMPANY_TOOLS_URLS_FILE', str(Path(__file__).with_name('company-tools-urls.example.json'))),
        CODEX_AUTH_BRIDGE_URL=os.environ.get('CODEX_AUTH_BRIDGE_URL'),
        CODEX_AUTH_BRIDGE_TOKEN_FILE=os.environ.get('CODEX_AUTH_BRIDGE_TOKEN_FILE'),
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', MAX_CONTENT_LENGTH=65536,
        SESSION_COOKIE_SECURE=False, EVENTS_WAIT_SECONDS=20)
    if config: app.config.update(config)
    if app.config['COMPANY_PROFILE_FILE'] is None:
        app.config['COMPANY_PROFILE_FILE']=str(Path(app.config['REPO'])/'deploy/single-host/company-identities.json')
    if not config or 'SESSION_COOKIE_SECURE' not in config:
        app.config['SESSION_COOKIE_SECURE'] = urlsplit(app.config['EXPERIMENTS_PUBLIC_URL']).scheme.lower() == 'https'
    if not app.secret_key: raise RuntimeError('EXPERIMENTS_SECRET_KEY must be configured')
    Path(app.config['DATABASE']).parent.mkdir(parents=True, exist_ok=True)
    @contextmanager
    def db():
        with closing(sqlite3.connect(app.config['DATABASE'], timeout=15 * _kernel_virtual_time_rate())) as connection:
            connection.row_factory=sqlite3.Row
            with connection:
                yield connection
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('''CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL,
          title TEXT NOT NULL, description TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
          room_id TEXT, message_id TEXT, error TEXT, messages TEXT NOT NULL DEFAULT '[]',
          revision INTEGER NOT NULL DEFAULT 0, idempotency_key TEXT UNIQUE NOT NULL,
          kind TEXT NOT NULL DEFAULT 'workplace', simulation_run_id TEXT,
          simulated_time TEXT, activity TEXT NOT NULL DEFAULT '[]',
          company_id TEXT, simulation_experiment_id TEXT, sender_employee_id TEXT)''')
        columns={item['name'] for item in c.execute('PRAGMA table_info(runs)')}
        for name, declaration in (('kind', "TEXT NOT NULL DEFAULT 'workplace'"),
                                  ('simulation_run_id', 'TEXT'), ('simulated_time', 'TEXT'),
                                  ('activity', "TEXT NOT NULL DEFAULT '[]'"), ('company_id','TEXT'),
                                  ('simulation_experiment_id','TEXT'),('provider_url','TEXT'),('provider_status','TEXT'),
                                  ('sender_employee_id','TEXT'),('chat_browser_url','TEXT')):
            if name not in columns: c.execute(f'ALTER TABLE runs ADD COLUMN {name} {declaration}')
        c.execute("""UPDATE runs SET status='uncertain',
          error=CASE WHEN kind='simulation' THEN 'The server restarted while dispatching. Retry will reconcile the same experiment ID.'
                     ELSE 'The server restarted while sending. Retry will reconcile the same message ID.' END,
          revision=revision+1 WHERE status IN ('queued','sending','initializing')""")
        c.execute('''CREATE TABLE IF NOT EXISTS company_state (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1), status TEXT NOT NULL,
          simulated_time TEXT, paused INTEGER NOT NULL DEFAULT 0, erp_site_id TEXT,
          company_id TEXT, experiments TEXT NOT NULL DEFAULT '[]',
          activity TEXT NOT NULL DEFAULT '[]', error TEXT, updated_at TEXT NOT NULL)''')
        company_columns={item['name'] for item in c.execute('PRAGMA table_info(company_state)')}
        for name, declaration in (('company_id','TEXT'),('experiments',"TEXT NOT NULL DEFAULT '[]'"),
                                  ('activity',"TEXT NOT NULL DEFAULT '[]'")):
            if name not in company_columns: c.execute(f'ALTER TABLE company_state ADD COLUMN {name} {declaration}')
        c.execute("INSERT OR IGNORE INTO company_state(singleton,status,updated_at) VALUES(1,'not_started',?)",
                  (datetime.now(timezone.utc).isoformat(),))
    experiments=catalog(app.config['REPO'])
    compatibility=public_compatibility()
    inputs=json.loads(Path(__file__).with_name('inputs.json').read_text())['tasks']
    rocket=client_factory()
    def client(): return rocket
    coordinator_instance=None
    coordinator_lock=threading.Lock()
    def coordinator():
        nonlocal coordinator_instance
        with coordinator_lock:
            if coordinator_instance is None: coordinator_instance=coordinator_factory()
            return coordinator_instance
    def imported_catalog():
        payload = coordinator().company_pack_catalog()
        entries = payload.get('experiments')
        if not isinstance(entries, list):
            raise CoordinatorError('company/packs/catalog', 'invalid_response')
        if any(not isinstance(entry, dict) or not isinstance(entry.get('id'), str)
               or not entry['id'] for entry in entries):
            raise CoordinatorError('company/packs/catalog', 'invalid_response')
        return {entry['id']: entry for entry in entries}

    def definition_for(experiment_id):
        definition = experiments.get(experiment_id)
        if definition is None:
            definition = imported_catalog().get(experiment_id)
        if definition is None:
            abort(404)
        return definition
    executor=ThreadPoolExecutor(max_workers=4, thread_name_prefix='experiments')
    app.extensions['experiments_executor']=executor
    changed=threading.Condition()
    version=[0]
    def notify():
        with changed:
            version[0]+=1
            changed.notify_all()
    def row(run_id):
        with db() as c: item=c.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if item is None: abort(404)
        data=dict(item);data['messages']=json.loads(data['messages'])
        data['activity']=json.loads(data['activity'])
        data.pop('idempotency_key',None)
        data['chat_url']=data.pop('chat_browser_url') if data['message_id'] else None
        data.pop('chat_browser_url',None)
        return data
    def all_runs():
        with db() as c: return [dict(r) for r in c.execute('SELECT * FROM runs ORDER BY created_at DESC')]
    def update(run_id, *, expected_revision=None, **values):
        material={key:value for key,value in values.items() if key!='simulated_time'}
        revision_clause=' AND revision=?' if expected_revision is not None else ''
        revision_params=(expected_revision,) if expected_revision is not None else ()
        with db() as c:
            changed_row=0
            if material:
                changed_row=c.execute(
                    'UPDATE runs SET '+','.join(k+'=?' for k in values)+',revision=revision+1 '
                    +'WHERE id=?'+revision_clause+' AND ('+' OR '.join(k+' IS NOT ?' for k in material)+')',
                    (*values.values(),run_id,*revision_params,*material.values()),
                ).rowcount
            if not changed_row and 'simulated_time' in values:
                c.execute('UPDATE runs SET simulated_time=? WHERE id=?'+revision_clause+' AND simulated_time IS NOT ?',
                          (values['simulated_time'],run_id,*revision_params,values['simulated_time']))
        if changed_row: notify()
    def save_company(state, error=None, *, clear_error=False):
        with db() as c:
            experiments=json.dumps(state.get('experiments',[]),sort_keys=True)
            activity=json.dumps(state.get('activity',[]),sort_keys=True)
            previous=c.execute('''SELECT status,simulated_time,paused,erp_site_id,company_id,
              experiments,activity,error FROM company_state WHERE singleton=1''').fetchone()
            if previous and not clear_error: error=previous['error']
            desired=(state.get('status','not_started'),state.get('simulated_time'),
                     int(bool(state.get('paused'))),state.get('erp_site_id'),state.get('company_id'),
                     experiments,activity,error)
            if previous and tuple(previous)==desired: return
            notify_change=(not previous or any(previous[index]!=desired[index] for index in (0,2,3,4,5,6,7)))
            c.execute('''UPDATE company_state SET status=?,simulated_time=?,paused=?,erp_site_id=?,company_id=?,
              experiments=?,activity=?,error=?,updated_at=? WHERE singleton=1''',
              (*desired,datetime.now(timezone.utc).isoformat()))
        if notify_change: notify()
    def save_company_error(message):
        with db() as c:
            previous=c.execute('SELECT error FROM company_state WHERE singleton=1').fetchone()['error']
            if previous==message: return
            c.execute('UPDATE company_state SET error=?,updated_at=? WHERE singleton=1',
                      (message,datetime.now(timezone.utc).isoformat()))
        notify()
    def safe_background(background):
        if background is not None:
            if not isinstance(background,dict):
                raise CoordinatorError('company','invalid_background_status')
            providers=background.get('providers',{})
            errors=background.get('errors',{})
            known_providers={'coffeebench','ecommercebench'}
            known_errors=known_providers | {
                'configuration','company_background','company_feedback_reconcile','native_finance_reconcile'
            }
            if not isinstance(providers,dict) or not isinstance(errors,dict):
                raise CoordinatorError('company','invalid_background_status')
            safe_providers={}
            for provider_id,value in providers.items():
                if provider_id not in known_providers or not isinstance(value,dict):
                    raise CoordinatorError('company','invalid_background_provider')
                provider_status=value.get('status')
                initialized=value.get('initialized')
                next_due=value.get('next_due')
                if (provider_status not in {'active','error','stopped','stopping','starting'} or not isinstance(initialized,bool)
                        or (next_due is not None and not isinstance(next_due,str))):
                    raise CoordinatorError('company','invalid_background_provider')
                safe_providers[provider_id]={'status':provider_status,'initialized':initialized,
                                             'next_due':next_due}
                for flag in ('enabled','in_flight'):
                    if flag in value:
                        if not isinstance(value[flag],bool):
                            raise CoordinatorError('company','invalid_background_provider')
                        safe_providers[provider_id][flag]=value[flag]
            safe_errors={}
            for provider_id,value in errors.items():
                if (provider_id not in known_errors or not isinstance(value,dict)
                        or not isinstance(value.get('code'),str)
                        or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,95}',value['code'])):
                    raise CoordinatorError('company','invalid_background_error')
                safe_errors[provider_id]={'code':value['code']}
            if not isinstance(background.get('initialized'),bool):
                raise CoordinatorError('company','invalid_background_status')
            background={'initialized':background['initialized'],'providers':safe_providers,
                        'errors':safe_errors}
        return background

    def safe_company(payload, *, require_activity=False):
        if not isinstance(payload,dict): raise CoordinatorError('company','invalid_response')
        allowed_status={'not_started','initializing','running','paused','failed','resetting'}
        status=payload.get('status','not_started')
        if (status not in allowed_status or not isinstance(payload.get('company_id'),str)
                or not isinstance(payload.get('paused'),bool)):
            raise CoordinatorError('company','invalid_status')
        if require_activity and (not isinstance(payload.get('experiments'),list)
                                or not isinstance(payload.get('activity'),list)
                                or not isinstance(payload.get('paused'),bool)):
            raise CoordinatorError('company','invalid_response')
        background=safe_background(payload.get('background'))
        experiment_items=payload.get('experiments',[])
        activity_items=payload.get('activity',[])
        if not isinstance(experiment_items,list) or not isinstance(activity_items,list):
            raise CoordinatorError('company','invalid_response')
        experiments=[]
        for item in experiment_items:
            if (not isinstance(item,dict) or not isinstance(item.get('experiment_id'),str)
                    or not isinstance(item.get('scenario_id'),str) or not isinstance(item.get('status'),str)):
                raise CoordinatorError('company','invalid_experiment_record')
            experiments.append({key:item[key] for key in
                ('experiment_id','scenario_id','agent_id','generation','status','simulated_time','paused',
                 'kind','provider_id','task_id')
                if key in item})
        activity=[]
        for item in activity_items:
            if (not isinstance(item,dict) or not all(key in item for key in ('id','timestamp','event'))
                    or not any(item.get(key) for key in ('description','summary','title','content'))):
                raise CoordinatorError('company','invalid_activity_record')
            if not item.get('description'):
                item={**item,'description':item.get('summary') or item.get('title') or item.get('content')}
            activity.append({key:item[key] for key in
                ('id','timestamp','event','description','experiment_id','scenario_id',
                 'type','title','summary','content','actor','recipient','related_activity_id','delivery_status','attachments','native_document','direction','received_at','created_at','coffee_update','can_resend') if key in item})
        clock_speed=payload.get('clock_speed')
        if clock_speed is not None and (isinstance(clock_speed,bool) or not isinstance(clock_speed,(int,float))
                                        or not math.isfinite(clock_speed) or clock_speed<=0):
            raise CoordinatorError('company','invalid_clock_speed')
        return {key:payload.get(key) for key in ('company_id','simulated_time','erp_site_id','clock_mode')} | {
            'background':background,
            'status':status,'paused':bool(payload.get('paused')),
            'experiments':experiments,'activity':activity,
            **({'clock_speed':float(clock_speed)} if clock_speed is not None else {})}
    def company_state(refresh=True):
        if refresh:
            state=safe_company(coordinator().company(),require_activity=True)
            # A confirmed snapshot resolves the dashboard request error. Native
            # provider/action failures remain in the authoritative payload.
            save_company(state,clear_error=True)
            return state
        with db() as c: item=dict(c.execute('SELECT * FROM company_state WHERE singleton=1').fetchone())
        return {'company_id':item['company_id'] or 'company','status':item['status'],'simulated_time':item['simulated_time'],
                'paused':bool(item['paused']),'erp_site_id':item['erp_site_id'],
                'experiments':json.loads(item['experiments']),'activity':json.loads(item['activity']),
                'error':item['error']}
    def api_company_shape(state):
        statuses={'not_started':'stopped','initializing':'starting','running':'running',
                  'paused':'paused','failed':'stopped','resetting':'starting'}
        return {'id':state.get('company_id') or 'company','name':json.loads(Path(app.config['COMPANY_PROFILE_FILE']).read_text())['company']['name'],
                'status':statuses.get(state.get('status'),'stopped'),
                'simulated_time':state.get('simulated_time'),'clock_mode':state.get('clock_mode'),'paused':bool(state.get('paused')),
                'erp_site_id':state.get('erp_site_id'),'experiments':state.get('experiments',[]),
                'activity':state.get('activity',[]),'background':state.get('background'),'error':state.get('error'),
                **({'clock_speed':state['clock_speed']} if 'clock_speed' in state else {})}
    def dispatch(run_id):
        with app.app_context():
            item=row(run_id)
            if item['kind']=='simulation':
                update(run_id,status='initializing',error=None)
                try:
                    state=company_state()
                except Exception as error:
                    logging.exception('Company status check failed before experiment dispatch run=%s',run_id)
                    update(run_id,status='failed',error=f'Company status could not be confirmed ({type(error).__name__}). Refresh company status, then retry initialization.')
                    return
                try:
                    definition=definition_for(item['experiment_id'])
                    start_kwargs={'idempotency_key':item['id']}
                    if definition.get('provider_id'):
                        started=coordinator().start_provider_experiment(
                            definition['provider_id'], definition['task_id'], **start_kwargs)
                    else:
                        if definition.get('task_id') is not None: start_kwargs['task_id']=definition['task_id']
                        started=coordinator().start_experiment(definition.get('scenario_id',item['experiment_id']),**start_kwargs)
                    simulation_run_id=started.get('experiment_id')
                    if not simulation_run_id: raise CoordinatorError('company/experiments','missing_experiment_id')
                    update(run_id,status='initializing' if definition.get('provider_id') else started.get('status','running'),simulation_run_id=simulation_run_id,
                           simulation_experiment_id=simulation_run_id,company_id=state.get('company_id'))
                    refresh_simulation(run_id)
                except CoordinatorError as error:
                    logging.exception('Simulation experiment dispatch failed run=%s',run_id)
                    if error.code in {'http_401','http_403','http_409','http_422'}:
                        if error.code=='http_409':
                            message='Company state rejected this experiment. Retry initialization; if this continues, contact the operator.'
                        elif error.code in {'http_401','http_403'}:
                            message='Coordinator authorization rejected the request. Contact the operator.'
                        else:
                            message='Coordinator rejected the experiment configuration. Contact the operator.'
                        update(run_id,status='failed',error=message)
                    else:
                        correlation=f' Correlation ID: {error.correlation_id}.' if error.correlation_id else ''
                        update(run_id,status='uncertain',error=f'Simulation start could not be confirmed ({type(error).__name__}). Retry checks the same experiment request before dispatch.{correlation}')
                except Exception as error:
                    logging.exception('Simulation dispatch failed run=%s',run_id)
                    update(run_id,status='uncertain',error=f'Simulation start could not be confirmed ({type(error).__name__}). Retry checks the same experiment request before dispatch.')
                return
            try:
                update(run_id,status='sending',error=None)
                source_files = inputs.get(item['experiment_id'], [])
                public_url = app.config['EXPERIMENTS_PUBLIC_URL']
                if source_files and not public_url:
                    raise RuntimeError('EXPERIMENTS_PUBLIC_URL must be configured for source-file links')
                files=[{'name':Path(f['source']).name,'url':public_url.rstrip('/')+f"/experiments/{item['experiment_id']}/inputs/{i}"} for i,f in enumerate(source_files)]
                definition=definition_for(item['experiment_id'])
                role=definition.get('role')
                if not isinstance(role,str) or not role:
                    raise CoordinatorError('experiment-chat','sender_department_unavailable')
                employee_id=item.get('sender_employee_id') or definition.get('sender_employee_id')
                employee_id,employee_user_id=company_experiment_sender(
                    role, employee_id, explicit_sender=bool(definition.get('sender_employee_id')))
                if not item.get('sender_employee_id'):
                    update(run_id,sender_employee_id=employee_id)
                sent=client().dispatch_employee_browser(
                    run_id,item['title'],item['description'],inputs=files,
                    role=role,employee_id=employee_id,employee_user_id=employee_user_id,
                    coordinator=coordinator(),
                )
                if sent.get('status')=='uncertain':
                    detail = {
                        'agent_no_cua_actions': 'The employee agent stopped without using CUA. No chat delivery was confirmed.',
                        'agent_chat_message_not_confirmed': 'The employee agent finished its CUA turn, but the requested chat message was not confirmed.',
                    }.get(sent.get('error_code'), 'Employee browser send is uncertain.')
                    message_id = sent.get('message_id')
                    if message_id:
                        detail += ' Retry checks the recorded Rocket.Chat message without sending again.'
                    else:
                        detail += ' No Rocket.Chat message ID was captured, so delivery cannot be confirmed automatically.'
                    update(run_id,status='uncertain',room_id=sent.get('room_id'),
                           message_id=message_id,error=detail)
                    return
                else:
                    if (sent.get('status')!='sent' or sent.get('employee_id')!=employee_id
                            or not isinstance(sent.get('room_id'),str) or not isinstance(sent.get('message_id'),str)):
                        raise CoordinatorError('experiment-chat','invalid_response')
                    update(run_id,status='awaiting_viv',room_id=sent['room_id'],
                           message_id=sent['message_id'],sender_employee_id=employee_id,
                           chat_browser_url=sent['browser_url'])
            except Exception as error:
                logging.exception('Experiment dispatch failed run=%s', run_id)
                if isinstance(error, CoordinatorError) and error.code == 'browser_send_failed_before_submit':
                    detail = 'The employee desktop failed before the message was submitted. Retry resumes this same request.'
                else:
                    code = f'{type(error).__name__}: {error.code}' if isinstance(error, CoordinatorError) else type(error).__name__
                    detail = f'Could not confirm delivery ({code}). Retry checks the same request before sending again.'
                update(run_id,status='uncertain',error=detail)
                return
            try: refresh(run_id)
            except Exception:
                logging.exception('Initial thread refresh failed run=%s',run_id)
                update(run_id,error='Request delivered. Chat updates could not be retrieved. Sync messages to retry.')
    def refresh_simulation(run_id):
        item=row(run_id)
        if item['kind']!='simulation' or not item['simulation_run_id']: return
        if item['experiment_id'].startswith('tau-provider-'):
            payload=coordinator().provider_experiment(item['simulation_run_id'])
            provider=payload.get('experiment')
            if not isinstance(provider,dict) or provider.get('experiment_id') != item['simulation_run_id']:
                raise CoordinatorError('provider-experiment','scope_mismatch')
            statuses={'preparing':'initializing', 'portal_registration_pending':'initializing', 'initialization_uncertain':'uncertain',
                      'portal_registration_uncertain':'uncertain', 'registered':'running',
                      'failed':'failed', 'completed':'completed'}
            if provider.get('status') not in statuses:
                raise CoordinatorError('provider-experiment','invalid_status')
            activity=provider.get('activity',[])
            if not isinstance(activity,list):
                raise CoordinatorError('provider-experiment','invalid_activity')
            descriptions={
                'preparing':'The company case is being prepared.',
                'portal_registration_pending':'The employee request is being delivered.',
                'initialization_uncertain':'The provider setup is awaiting reconciliation.',
                'portal_registration_uncertain':'The employee request is awaiting delivery confirmation.',
                'registered':'The company request was delivered.',
                'failed':'The provider case could not be initialized.',
                'completed':'The provider case was completed.',
                'email_delivered':'An email was delivered.',
                'email_received':'An email reply was received.',
                'email_pending':'An email is waiting for delivery.',
                'email_uncertain':'Email delivery is awaiting confirmation.',
            }
            safe_activity=[]
            for index,entry in enumerate(activity):
                if not isinstance(entry,dict) or entry.get('status') not in descriptions:
                    raise CoordinatorError('provider-experiment','invalid_activity')
                timestamp=entry.get('received_at') or entry.get('occurred_at') or entry.get('created_at')
                if not isinstance(timestamp,str):
                    raise CoordinatorError('provider-experiment','invalid_activity_timestamp')
                event={'id':f'{item["simulation_run_id"]}:{index}',
                    'timestamp':timestamp,'event':entry['status'],
                    'description':descriptions[entry['status']]}
                if entry['status'].startswith('email_'):
                    if entry.get('direction') not in {'sent','received'}:
                        raise CoordinatorError('provider-experiment','invalid_email_direction')
                    event.update(type='email',direction=entry['direction'])
                    for key in ('subject','body','message_id'):
                        if key in entry and not isinstance(entry[key],str):
                            raise CoordinatorError('provider-experiment','invalid_email_activity')
                    event.update(content=entry.get('body',''),title=entry.get('subject',''),
                                 summary=entry.get('subject',''))
                safe_activity.append(event)
            portal_url=provider.get('portal_url')
            if portal_url is not None and (not isinstance(portal_url,str) or urlsplit(portal_url).scheme not in {'http','https'}):
                raise CoordinatorError('provider-experiment','invalid_portal_url')
            status=statuses[provider['status']]
            error=('Provider setup could not be confirmed. Retry initialization checks the same case.' if status=='uncertain'
                   else 'Provider setup failed. Retry initialization after the integration issue is repaired.' if status=='failed' else None)
            update(run_id,status=status,provider_status=provider['status'],provider_url=portal_url,
                   activity=json.dumps(safe_activity),error=error)
            return
        state=company_state()
        linked=next((entry for entry in state['experiments']
                     if entry.get('experiment_id')==item['simulation_run_id']),None)
        if linked is None: raise CoordinatorError('company','linked_experiment_not_found')
        paused=bool(linked.get('paused',state['paused']))
        status=linked.get('status','running')
        if status not in {'completed','failed'} and (paused or status=='paused'):
            status='paused'
        if status not in {'initializing','running','paused','completed','failed','resetting'}:
            raise CoordinatorError('company','invalid_experiment_status')
        activity=[entry for entry in state['activity']
                  if entry.get('experiment_id')==item['simulation_run_id']]
        update(run_id,status=status,simulated_time=linked.get('simulated_time'),
               activity=json.dumps(activity),error=None)
    def refresh(run_id):
        item=row(run_id)
        if not item['message_id']: return
        chat=client()
        messages=chat.thread(item['room_id'],item['message_id'])
        if messages != item['messages'] or item['error']:
            status=item['status']
            if status!='completed' and any(m['author']==chat.viv_username for m in messages): status='viv_replied'
            update(run_id,expected_revision=item['revision'],messages=json.dumps(messages),status=status,error=None)
    def refresh_channel(room_id):
        with app.app_context():
            for item in all_runs():
                if item['room_id']==room_id:
                    try: refresh(item['id'])
                    except Exception:
                        logging.exception('Thread refresh failed run=%s',item['id'])
                        update(item['id'],error='Chat updates could not be retrieved. Refresh to retry.')
    @app.before_request
    def request_csrf():
        if 'csrf_token' not in session: session['csrf_token']=secrets.token_urlsafe(32)
        if request.endpoint == 'api_upload_company_pack':
            # Flask's per-request limit preserves the small default for other endpoints.
            request.max_content_length = 50 * 1024 * 1024 + 65536
        if request.method=='POST' and request.endpoint!='chat_events':
            json_body=request.get_json(silent=True) if request.is_json else None
            token=request.form.get('csrf_token','')
            if isinstance(json_body,dict): token=json_body.get('csrf_token',token)
            if not secrets.compare_digest(token,session['csrf_token']): abort(400)
    def frontend():
        return app.send_static_file('app/index.html')
    def run_response(run_id):
        location=url_for('run_detail',run_id=run_id)
        if request.accept_mimetypes.best=='application/json':
            return jsonify(run_id=run_id,url=location)
        return redirect(location,code=303)
    @app.get('/')
    @app.get('/streams')
    @app.get('/help')
    @app.get('/help/company-environment')
    @app.get('/settings/company-clock')
    @app.get('/help/context')
    @app.get('/help/native-evaluations')
    @app.get('/help/company-tools')
    @app.get('/experiments/background')
    @app.get('/experiments/ecommercebench/settings')
    @app.get('/experiments/coffeebench/settings')
    @app.get('/experiments')
    def index(): return frontend()
    @app.get('/api/session')
    def api_session(): return jsonify(csrf_token=session['csrf_token'],idempotency_key=str(uuid.uuid4()))
    @app.get('/api/catalog')
    def api_catalog():
        try:
            return jsonify(experiments=list((experiments | imported_catalog()).values()))
        except CoordinatorError as error:
            logging.exception('Imported experiment catalog unavailable code=%s', error.code)
            return jsonify(experiments=list(experiments.values()),
                           error={'code': error.code, 'correlation_id': error.correlation_id})

    def company_pack_error(error):
        logging.exception('Company pack request failed code=%s', error.code)
        status = {'pack_invalid_archive': 422, 'pack_version_conflict': 409,
                  'pack_clock_unavailable': 503}.get(error.code, 503)
        if re.fullmatch(r'http_4[0-9]{2}', error.code):
            status = int(error.code.removeprefix('http_'))
        return jsonify(error={'code': error.code, 'correlation_id': error.correlation_id}), status

    @app.get('/api/company-environments')
    def api_company_environments():
        try:
            return jsonify(coordinator().company_environments())
        except CoordinatorError as error:
            return company_pack_error(error)

    @app.post('/api/company-environments/clone')
    def api_clone_company_environment():
        key = request.form.get('idempotency_key')
        name = request.form.get('name')
        if not key or not name: abort(400)
        try:
            return jsonify(coordinator().clone_company_environment(name, idempotency_key=key))
        except CoordinatorError as error:
            return company_pack_error(error)

    @app.post('/api/company-environments/<identifier>/switch')
    def api_switch_company_environment(identifier):
        key = request.form.get('idempotency_key')
        if not key: abort(400)
        try:
            return jsonify(coordinator().switch_company_environment(identifier, idempotency_key=key))
        except CoordinatorError as error:
            return company_pack_error(error)

    @app.post('/api/company-environments/<identifier>/rename')
    def api_rename_company_environment(identifier):
        name = request.form.get('name')
        if not name: abort(400)
        try:
            return jsonify(coordinator().rename_company_environment(identifier, name))
        except CoordinatorError as error:
            return company_pack_error(error)

    @app.get('/api/company-packs')
    def api_company_packs():
        try:
            return jsonify(coordinator().company_packs())
        except CoordinatorError as error:
            return company_pack_error(error)

    @app.post('/api/company-packs')
    def api_upload_company_pack():
        archive = request.files.get('file')
        key = request.headers.get('Idempotency-Key')
        if archive is None or not key: abort(400)
        try:
            return jsonify(coordinator().upload_company_pack(archive.stream, idempotency_key=key))
        except CoordinatorError as error:
            return company_pack_error(error)

    @app.get('/api/company-packs/<pack_id>')
    def api_company_pack(pack_id):
        try:
            return jsonify(coordinator().company_pack(pack_id))
        except CoordinatorError as error:
            return company_pack_error(error)

    @app.post('/api/company-packs/<pack_id>/import')
    def api_import_company_pack(pack_id):
        key = request.form.get('idempotency_key')
        if not key: abort(400)
        try:
            return jsonify(coordinator().import_company_pack(pack_id, idempotency_key=key))
        except CoordinatorError as error:
            return company_pack_error(error)

    @app.get('/api/company-packs/<pack_id>/events')
    def api_company_pack_events(pack_id):
        def stream():
            # Status is durable upstream. Bound this condition polling stream to 15 minutes;
            # reconnect is explicit in the UI, and no database transaction is held waiting.
            deadline = time.monotonic() + 900
            wait = threading.Event()
            while time.monotonic() < deadline:
                try:
                    pack = coordinator().company_pack(pack_id)
                except CoordinatorError as error:
                    logging.exception('Company pack status stream failed code=%s', error.code)
                    yield 'event: error\ndata: ' + json.dumps({'code': error.code}) + '\n\n'
                    return
                yield 'data: ' + json.dumps(pack) + '\n\n'
                if pack['status'] not in {'queued', 'importing'}: return
                wait.wait(min(1, max(0, deadline - time.monotonic())))
            yield 'event: error\ndata: {"code":"pack_status_stream_expired"}\n\n'
        return Response(stream(), mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})
    @app.get('/api/compatibility')
    def api_compatibility(): return jsonify(compatibility)
    @app.get('/api/company-tools')
    def api_company_tools():
        try:
            mapping = company_tools_urls(app.config['COMPANY_TOOLS_URLS_FILE'])
            applications = coordinator().company_packs()['applications']
            enabled = set(applications) if applications is not None else set(mapping)
            links = [{'id': key, 'name': name, 'url': mapping[key]}
                     for key, name in COMPANY_TOOLS if key in enabled]
        except CoordinatorError as error:
            logging.exception('Company application selection unavailable code=%s', error.code)
            return jsonify(error={'code': 'company_tools_coordinator_unavailable',
                                  'correlation_id': error.correlation_id}), 503
        except (OSError, KeyError, TypeError, ValueError) as error:
            logging.exception('Company tools URL mapping unavailable (%s)', type(error).__name__)
            return jsonify(error='Company tools URL mapping is unavailable or invalid.'), 503
        return jsonify(tools=links)

    def codex_auth_json(payload, status=200):
        response=jsonify(payload)
        response.headers['Cache-Control']='no-store'
        return response,status

    def codex_auth_bridge(path, method='GET', payload=None):
        token_file=app.config.get('CODEX_AUTH_BRIDGE_TOKEN_FILE')
        bridge_url=app.config.get('CODEX_AUTH_BRIDGE_URL') or (
            'http://10.0.2.2:8143' if token_file else None
        )
        if not bridge_url or not token_file:
            return None, codex_auth_json({'error':{'code':'codex_auth_bridge_unconfigured'}},503)
        try:
            parsed=urlsplit(bridge_url)
            if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('invalid bridge URL')
            token=Path(token_file).read_text(encoding='utf-8').strip()
            if not token:
                raise ValueError('empty bridge bearer token')
        except (OSError,TypeError,ValueError) as error:
            logging.error('Codex auth bridge configuration unavailable exception_type=%s',type(error).__name__)
            return None, codex_auth_json({'error':{'code':'codex_auth_bridge_unavailable'}},503)
        try:
            response=requests.request(
                method,bridge_url.rstrip('/')+path,
                headers={'Authorization':f'Bearer {token}','Accept':'application/json'},
                json=payload,timeout=None,allow_redirects=False,
            )
        except requests.RequestException as error:
            logging.warning('Codex auth bridge request failed exception_type=%s',type(error).__name__)
            return None, codex_auth_json({'error':{'code':'codex_auth_bridge_unavailable'}},502)
        if not 200 <= response.status_code < 300:
            logging.warning('Codex auth bridge returned status=%s',response.status_code)
            return None, codex_auth_json({'error':{'code':'codex_auth_bridge_rejected'}},502)
        try:
            data=response.json()
            if not isinstance(data, dict):
                raise ValueError('invalid response')
            if path == '/v1/model-control':
                if not isinstance(data.get('enabled'), bool):
                    raise ValueError('invalid model control status')
                return {'enabled': data['enabled']}, None
            if data.get('state') not in {'authenticated','unauthenticated','starting','pending','completed','failed','expired'}:
                raise ValueError('invalid response state')
            safe={'state':data['state']}
            if 'authenticated' in data:
                if not isinstance(data['authenticated'],bool):
                    raise ValueError('invalid authenticated status')
                safe['authenticated']=data['authenticated']
            for field in ('auth_method','login_id','user_code','expires_at'):
                value=data.get(field)
                if value is not None:
                    if not isinstance(value,str):
                        raise ValueError('invalid response field')
                    safe[field]=value
            verification_url=data.get('verification_url')
            if verification_url is not None:
                if not isinstance(verification_url,str):
                    raise ValueError('invalid verification URL')
                verification=urlsplit(verification_url)
                if verification.scheme not in {'http','https'} or not verification.hostname or verification.username or verification.password:
                    raise ValueError('invalid verification URL')
                safe['verification_url']=verification_url
            error_code=data.get('error')
            if error_code is not None:
                if not isinstance(error_code,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,95}',error_code):
                    raise ValueError('invalid error code')
                safe['error']=error_code
            return safe, None
        except (ValueError,requests.JSONDecodeError) as error:
            logging.warning('Codex auth bridge returned an invalid response exception_type=%s',type(error).__name__)
            return None, codex_auth_json({'error':{'code':'codex_auth_bridge_invalid_response'}},502)

    @app.get('/api/company/model-control')
    def api_company_model_control_status():
        result, failure = codex_auth_bridge('/v1/model-control')
        return failure if failure else codex_auth_json(result)

    @app.post('/api/company/model-control')
    def api_company_model_control():
        raw = request.form.get('enabled')
        if raw not in {'true', 'false'}:
            abort(400)
        result, failure = codex_auth_bridge('/v1/model-control', method='POST', payload={'enabled': raw == 'true'})
        return failure if failure else codex_auth_json(result)

    @app.get('/api/company/model-auth/status')
    def api_company_model_auth_status():
        result,failure=codex_auth_bridge('/auth/status')
        return failure if failure else codex_auth_json(result)

    @app.post('/api/company/model-auth/device/start')
    def api_company_model_auth_device_start():
        result,failure=codex_auth_bridge('/auth/device/start',method='POST',payload={})
        return failure if failure else codex_auth_json(result)

    @app.get('/api/company/model-auth/device/<login_id>')
    def api_company_model_auth_device_status(login_id):
        result,failure=codex_auth_bridge('/auth/device/'+quote(login_id,safe=''))
        return failure if failure else codex_auth_json(result)
    @app.get('/api/company/clock')
    def api_company_clock():
        try:
            clock=coordinator().company_clock()
            return jsonify(simulated_time=clock['simulated_time'], clock_mode=clock['clock_mode'],
                           clock_speed=clock.get('clock_speed'), can_set_speed=clock.get('can_set_speed', False))
        except Exception:
            logging.exception('Company clock retrieval failed')
            return jsonify(error='Company clock could not be retrieved.'),503

    @app.get('/api/company/background')
    def api_company_background():
        try:
            background=safe_background(coordinator().company_background())
            if background is None:
                raise CoordinatorError('company/background','invalid_background_status')
            if 'configuration' in background['errors']:
                raise CoordinatorError('company/background',background['errors']['configuration']['code'])
            providers=background['providers']
            if (set(providers)!={'coffeebench','ecommercebench'}
                    or any(not {'enabled','in_flight'} <= provider.keys() for provider in providers.values())):
                raise CoordinatorError('company/background','incomplete_benchmark_status')
            return jsonify(background=background)
        except Exception:
            logging.exception('Benchmark status retrieval failed')
            return jsonify(error='Benchmark status could not be loaded. Refresh to retry.'),503

    @app.get('/api/company')
    def api_company():
        try:
            state=company_state()
        except Exception as error:
            logging.exception('Company status retrieval failed')
            save_company_error(f'Company status could not be retrieved ({type(error).__name__}). Refresh to retry.')
            return jsonify(error='Company status could not be retrieved. Refresh to retry.'),503
        with db() as c: stored_error=c.execute('SELECT error FROM company_state WHERE singleton=1').fetchone()['error']
        state['error']=stored_error
        return jsonify(company=api_company_shape(state))

    def company_employee_roster():
        path=Path(app.config['COMPANY_PROFILE_FILE'])
        try:
            document=json.loads(path.read_text(encoding='utf-8'))
            employees=document['employees']
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise CoordinatorError('company/desktops','employee_roster_unavailable') from error
        if not isinstance(employees,list):
            raise CoordinatorError('company/desktops','invalid_employee_roster')
        roster={}
        for employee in employees:
            if (not isinstance(employee,dict) or not isinstance(employee.get('id'),str)
                    or not employee['id']
                    or not isinstance(employee.get('displayName'),str) or not employee['displayName'].strip()
                    or not isinstance(employee.get('department'),str) or not employee['department'].strip()
                    or employee['id'] in roster):
                raise CoordinatorError('company/desktops','invalid_employee_roster')
            roster[employee['id']]={
                'id':employee['id'], 'name':employee['displayName'].strip(),
                'role':employee['department'].strip(),
            }
        return roster

    def company_experiment_sender(role, employee_id=None, *, explicit_sender=False):
        path=Path(app.config['COMPANY_PROFILE_FILE'])
        try:
            document=json.loads(path.read_text(encoding='utf-8'))
            company=document['company']
            employees=document['employees']
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise CoordinatorError('experiment-chat','company_identity_profile_unavailable') from error
        if not isinstance(company,dict) or not isinstance(employees,list):
            raise CoordinatorError('experiment-chat','company_identity_profile_invalid')
        department_map=company.get('experimentDepartmentMap')
        department=department_map.get(role) if isinstance(department_map,dict) else None
        if department is None and explicit_sender:
            department=next((item.get('department') for item in employees
                             if isinstance(item,dict) and item.get('department')==role),None)
        if not isinstance(department,str) or not department:
            raise CoordinatorError('experiment-chat','sender_department_unmapped')
        candidates=[]
        for employee in employees:
            if not isinstance(employee,dict):
                continue
            if employee.get('department') != department:
                continue
            account=employee.get('accounts',{}).get('rocketchat') if isinstance(employee.get('accounts'),dict) else None
            if (isinstance(employee.get('id'),str) and employee['id']
                    and isinstance(employee.get('email'),str) and employee['email']
                    and isinstance(account,dict) and isinstance(account.get('id'),str) and account['id']
                    and isinstance(account.get('username'),str) and account['username']):
                candidates.append((employee['id'],account['id']))
        if not candidates:
            raise CoordinatorError('experiment-chat','department_employee_unavailable')
        if employee_id is not None:
            match=next((candidate for candidate in candidates if candidate[0]==employee_id),None)
            if match is None:
                raise CoordinatorError('experiment-chat','sender_identity_mismatch')
            return match
        return candidates[0]

    def safe_desktop_status(value, *, id_field='employee_id'):
        if not isinstance(value,dict):
            raise CoordinatorError('company/desktops','invalid_response')
        employee_id=value.get(id_field)
        status=value.get('status')
        if not isinstance(employee_id,str) or not employee_id or not isinstance(status,str) or not status:
            raise CoordinatorError('company/desktops','invalid_response')
        return employee_id,status

    def safe_desktop_viewing_url(value, operation='company/desktops/start'):
        if not isinstance(value,str):
            raise CoordinatorError(operation,'invalid_viewing_url')
        try:
            parsed=urlsplit(value)
            # Access URLs are user-facing links; reject embedded credentials and
            # query parameters that commonly carry bearer/session secrets.
            if (parsed.scheme.lower() not in {'http','https'} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None):
                raise ValueError('unsafe URL')
            parsed.port
            if any(re.search(r'(token|secret|password|credential|authorization|api.?key)',key,re.I)
                   for key,_ in parse_qsl(parsed.query,keep_blank_values=True)):
                raise ValueError('secret-bearing query')
            if re.search(r'(token|secret|password|credential|authorization|api.?key)\s*[=:]',parsed.fragment,re.I):
                raise ValueError('secret-bearing fragment')
        except ValueError as error:
            raise CoordinatorError(operation,'invalid_viewing_url') from error
        return value

    @app.get('/api/company/desktops')
    def api_company_desktops():
        try:
            roster=company_employee_roster()
            payload=coordinator().company_desktops()
            entries=payload.get('employees')
            if not isinstance(entries,list):
                raise CoordinatorError('company/desktops','invalid_response')
            states={}
            for entry in entries:
                employee_id,status=safe_desktop_status(entry,id_field='id')
                if employee_id not in roster or employee_id in states:
                    raise CoordinatorError('company/desktops','invalid_employee_status')
                state={'status':status}
                viewing_url=entry.get('viewing_url')
                if viewing_url is not None:
                    if status.lower() not in {'running','active','ready'}:
                        raise CoordinatorError('company/desktops','unexpected_viewing_url')
                    state['viewing_url']=safe_desktop_viewing_url(
                        viewing_url,operation='company/desktops')
                states[employee_id]=state
            response=jsonify(employees=[{**employee,**states.get(employee_id,{'status':'unknown'})}
                                        for employee_id,employee in roster.items()])
            response.headers['Cache-Control']='no-store'
            return response
        except CoordinatorError as error:
            logging.exception('Company desktop list failed code=%s',error.code)
            status=503 if error.code in {'employee_roster_unavailable','http_503','timeout','transport_error'} else 502
            return jsonify(error={'code':error.code,'correlation_id':error.correlation_id}),status
        except Exception as error:
            logging.exception('Company desktop list failed')
            return jsonify(error={'code':'desktop_service_unavailable'}),503

    @app.post('/api/company/desktops/start')
    def api_start_company_desktop():
        data=request.get_json(silent=True)
        if not isinstance(data,dict): abort(400)
        employee_id=data.get('employee_id')
        key=data.get('idempotency_key')
        if not isinstance(employee_id,str) or not employee_id or not isinstance(key,str) or not key: abort(400)
        try:
            roster=company_employee_roster()
            employee=roster.get(employee_id)
            if employee is None:
                return jsonify(error={'code':'unknown_employee'}),404
            result=coordinator().start_company_desktop(employee_id,idempotency_key=key)
            if set(result)!={'employee_id','status','viewing_url','viewing_credentials'}:
                raise CoordinatorError('company/desktops/start','invalid_response')
            returned_id,status=safe_desktop_status(result)
            if returned_id!=employee_id:
                raise CoordinatorError('company/desktops/start','employee_mismatch')
            viewing_url=safe_desktop_viewing_url(result.get('viewing_url'))
            response=jsonify(employee_id=employee_id,status=status,viewing_url=viewing_url)
            response.headers['Cache-Control']='no-store'
            notify()
            return response
        except CoordinatorError as error:
            logging.exception('Company desktop start failed employee_id=%s code=%s',employee_id,error.code)
            status=404 if error.code=='http_404' else 409 if error.code in {'http_409','http_422'} else 503 if error.code in {'http_503','timeout','transport_error'} else 502
            return jsonify(error={'code':error.code,'correlation_id':error.correlation_id}),status
        except Exception as error:
            logging.exception('Company desktop start failed employee_id=%s',employee_id)
            return jsonify(error={'code':'desktop_start_unavailable'}),503

    @app.post('/api/company/desktops/stop')
    def api_stop_company_desktop():
        data=request.get_json(silent=True)
        if not isinstance(data,dict): abort(400)
        employee_id=data.get('employee_id')
        key=data.get('idempotency_key')
        if not isinstance(employee_id,str) or not employee_id or not isinstance(key,str) or not key: abort(400)
        try:
            if employee_id not in company_employee_roster():
                return jsonify(error={'code':'unknown_employee'}),404
            result=coordinator().stop_company_desktop(employee_id,idempotency_key=key)
            if set(result)!={'employee_id','status'}:
                raise CoordinatorError('company/desktops/stop','invalid_response')
            returned_id,status=safe_desktop_status(result)
            if returned_id!=employee_id:
                raise CoordinatorError('company/desktops/stop','employee_mismatch')
            notify()
            return jsonify(employee_id=employee_id,status=status)
        except CoordinatorError as error:
            logging.exception('Company desktop stop failed employee_id=%s code=%s',employee_id,error.code)
            status=404 if error.code=='http_404' else 409 if error.code in {'http_409','http_422'} else 503 if error.code in {'http_503','timeout','transport_error'} else 502
            return jsonify(error={'code':error.code,'correlation_id':error.correlation_id}),status
        except Exception as error:
            logging.exception('Company desktop stop failed employee_id=%s',employee_id)
            return jsonify(error={'code':'desktop_stop_unavailable'}),503

    def safe_company_evaluation(value, provider_id):
        """Allowlist company-global native result fields without exposing run internals."""
        if not isinstance(value, dict):
            raise CoordinatorError('company/evaluations', 'invalid_response')
        allowed = {
            'evaluation_id', 'company_id', 'provider_id', 'source_id',
            'initialization_request_hash', 'upstream_commit', 'seed', 'started_at',
            'money_metric_units', 'native_state_ref', 'native_state_hash', 'native_company_time',
            'evaluation_mode', 'scope', 'attribution', 'requested_at', 'evaluated_at',
            'status', 'scores', 'criterion_results', 'warnings', 'mapping_gaps',
            'native_scale', 'evidence_ref', 'evidence_hash', 'correlation_id', 'failure',
            'lifecycle',
        }
        result = {key: value[key] for key in allowed if key in value}
        required = ('evaluation_id', 'company_id', 'provider_id', 'scope', 'attribution', 'status')
        if any(not isinstance(result.get(key), str) for key in required):
            raise CoordinatorError('company/evaluations', 'invalid_response')
        if (result['company_id'] != 'company' or result['provider_id'] != provider_id
                or result['scope'] != 'company_global'
                or result['attribution'] != 'company_global_not_experiment'):
            raise CoordinatorError('company/evaluations', 'scope_mismatch')
        if result['status'] not in {'completed', 'incomplete', 'failed'}:
            raise CoordinatorError('company/evaluations', 'invalid_status')
        if result.get('evaluation_mode') not in (None, 'company_adapted'):
            raise CoordinatorError('company/evaluations', 'invalid_mode')
        if result.get('evaluation_mode') is None and result['status'] != 'failed':
            raise CoordinatorError('company/evaluations', 'missing_mode')
        for key in ('source_id', 'initialization_request_hash', 'native_state_hash', 'evidence_hash'):
            if key in result and (not isinstance(result[key], str)
                                  or not re.fullmatch(r'[0-9a-f]{64}', result[key])):
                raise CoordinatorError('company/evaluations', 'invalid_source_hash')
        if 'upstream_commit' in result and (not isinstance(result['upstream_commit'], str)
                                             or not re.fullmatch(r'[0-9a-f]{40}', result['upstream_commit'])):
            raise CoordinatorError('company/evaluations', 'invalid_provider_revision')
        if 'seed' in result and (type(result['seed']) is not int or result['seed'] < 0):
            raise CoordinatorError('company/evaluations', 'invalid_source_seed')
        for key in ('started_at', 'native_company_time', 'requested_at', 'evaluated_at'):
            if result.get(key) is not None:
                if not isinstance(result[key], str):
                    raise CoordinatorError('company/evaluations', 'invalid_timestamp')
                try:
                    parsed = datetime.fromisoformat(result[key].replace('Z', '+00:00'))
                except ValueError as error:
                    raise CoordinatorError('company/evaluations', 'invalid_timestamp') from error
                if parsed.tzinfo is None or parsed.utcoffset() is None:
                    raise CoordinatorError('company/evaluations', 'invalid_timestamp')
        if 'native_state_ref' in result and (not isinstance(result['native_state_ref'], str)
                                              or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', result['native_state_ref'])):
            raise CoordinatorError('company/evaluations', 'invalid_state_reference')
        if 'money_metric_units' in result:
            expected_units = ({'audit.annual.true_net_income': 'USD'} if provider_id == 'coffeebench'
                              else {'profitability': 'GBP'})
            if result['money_metric_units'] != expected_units:
                raise CoordinatorError('company/evaluations', 'invalid_money_units')
        if 'scores' in result:
            scores = result['scores']
            if not isinstance(scores, dict) or any(
                not isinstance(key, str) or score is not None and not isinstance(score, (int, float, str, bool))
                for key, score in scores.items()
            ):
                raise CoordinatorError('company/evaluations', 'invalid_scores')
        if 'criterion_results' in result:
            criteria = result['criterion_results']
            if not isinstance(criteria, list) or any(not isinstance(item, dict) for item in criteria):
                raise CoordinatorError('company/evaluations', 'invalid_criteria')
            result['criterion_results'] = [{key: item[key] for key in (
                'criterion_id', 'criterion', 'name', 'status', 'score', 'value', 'actual',
                'expected', 'maximum', 'max_score', 'weight', 'gap_code', 'passed',
            ) if key in item and (item[key] is None or isinstance(item[key], (str, int, float, bool)))}
                for item in criteria]
        def safe_code(code):
            return isinstance(code, str) and bool(re.fullmatch(r'[A-Za-z0-9_.:-]{1,96}', code))
        for field in ('warnings', 'mapping_gaps'):
            if field in result and (not isinstance(result[field], list)
                                    or any(not safe_code(item) for item in result[field])):
                raise CoordinatorError('company/evaluations', f'invalid_{field}')
        if 'native_scale' in result:
            allowed_scales = {'boolean', 'categorical', 'seconds', 'native_reward_0_to_1', 'count', 'native_metric'}
            if not isinstance(result['native_scale'], dict) or any(
                not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', key)
                or not isinstance(scale, str) or scale not in allowed_scales
                for key, scale in result['native_scale'].items()
            ):
                raise CoordinatorError('company/evaluations', 'invalid_native_scale')
        if result.get('lifecycle') not in (None, 'reserved', 'running', 'uncertain'):
            raise CoordinatorError('company/evaluations', 'invalid_lifecycle')
        if result.get('failure') is not None:
            failure = result['failure']
            if not isinstance(failure, dict):
                raise CoordinatorError('company/evaluations', 'invalid_failure')
            result['failure'] = {key: failure[key] for key in ('code', 'exception_type')
                                 if safe_code(failure.get(key))}
        return result

    def company_evaluation_provider_history(provider_id):
        if provider_id not in COMPANY_EVALUATION_PROVIDERS:
            abort(404)
        history_payload = coordinator().company_evaluations(provider_id)
        rows = history_payload.get('evaluations')
        if history_payload.get('scope') != 'company_global' or not isinstance(rows, list):
            raise CoordinatorError('company/evaluations', 'invalid_history')
        evaluations = [safe_company_evaluation(item, provider_id) for item in rows]
        latest_payload = coordinator().latest_company_evaluation(provider_id)
        latest = latest_payload.get('evaluation')
        selection = latest_payload.get('selection')
        if selection != 'latest_saved':
            raise CoordinatorError('company/evaluations', 'invalid_latest_selection')
        selected = safe_company_evaluation(latest, provider_id) if latest is not None else None
        if selected is not None and all(item['evaluation_id'] != selected['evaluation_id'] for item in evaluations):
            raise CoordinatorError('company/evaluations', 'latest_not_in_history')
        return {
            'provider_id': provider_id,
            'evaluations': evaluations,
            'latest': selected,
            'selection': selection,
        }

    @app.get('/api/company/evaluations')
    def api_company_evaluations():
        providers = {}
        for provider_id in COMPANY_EVALUATION_PROVIDERS:
            try:
                providers[provider_id] = company_evaluation_provider_history(provider_id)
            except CoordinatorError as error:
                providers[provider_id] = {
                    'provider_id': provider_id,
                    'evaluations': [], 'latest': None, 'selection': 'latest_saved',
                    'error': {'code': error.code, 'correlation_id': error.correlation_id},
                }
        return jsonify(scope='company_global', attribution='company_global_not_experiment', providers=providers)

    @app.post('/api/company/evaluations/<provider_id>')
    def api_start_company_evaluation(provider_id):
        if provider_id not in COMPANY_EVALUATION_PROVIDERS:
            abort(404)
        key = request.form.get('idempotency_key')
        if not key:
            abort(400)
        try:
            payload = coordinator().start_company_evaluation(provider_id, idempotency_key=key)
            evaluation = payload.get('evaluation')
            return jsonify(evaluation=safe_company_evaluation(evaluation, provider_id))
        except CoordinatorError as error:
            status = 404 if error.code == 'http_404' else 409 if error.code == 'http_409' else 503 if error.code == 'http_503' else 502
            return jsonify(error={'code': error.code, 'correlation_id': error.correlation_id}), status
    @app.post('/company/start')
    def start_company():
        requested_key=request.form.get('idempotency_key')
        if not requested_key: abort(400)
        try:
            current=company_state()
            if current['status']=='paused':
                state=safe_company(coordinator().pause_company(False,idempotency_key=requested_key))
                state.update(experiments=current['experiments'],activity=current['activity'])
                save_company(state,clear_error=True)
                return jsonify(company=api_company_shape(state))
            if current['status']=='running':
                return jsonify(company=api_company_shape(current))
            return jsonify(error='The persistent company is not initialized. Retry after the coordinator is ready.'),503
        except Exception as error:
            logging.exception('Company resume failed')
            save_company_error(f'Company resume could not be confirmed ({type(error).__name__}). Retry to reconcile the requested state.')
            return jsonify(error='Company resume could not be confirmed. Retry to reconcile the requested state.'),502
    @app.post('/company/pause')
    def pause_company():
        raw=request.form.get('paused')
        key=request.form.get('idempotency_key')
        if raw not in {'true','false'} or not key: abort(400)
        try:
            state=safe_company(coordinator().pause_company(raw=='true',idempotency_key=key))
            save_company(state)
            return jsonify(company=api_company_shape(state))
        except Exception as error:
            logging.exception('Company pause update failed')
            message=f'Company pause state could not be confirmed ({type(error).__name__}). Refresh to retry.'
            save_company_error(message)
            return jsonify(error=message),502
    @app.route('/api/company/background/ecommercebench/behaviours', methods=['GET', 'POST'])
    def ecommerce_behaviours():
        args = ()
        if request.method == 'POST':
            behaviour, enabled = (request.form.get(key) for key in ('behaviour', 'enabled'))
            if behaviour not in {
                'cancellation_after_picking', 'stockout', 'parcel_loss', 'partial_return',
                'refund_after_payout', 'payout_mismatch',
            } or enabled not in {'true', 'false'}:
                abort(400)
            args = (behaviour, enabled == 'true')
        try:
            return jsonify(coordinator().ecommerce_behaviours(*args))
        except CoordinatorError:
            logging.exception('E-CommerceBench behaviours unavailable')
            return jsonify(error='E-CommerceBench behaviours could not be confirmed.'), 502

    @app.route('/api/company/background/coffeebench/payment-windows', methods=['GET', 'POST'])
    def coffee_payment_windows():
        args = ()
        if request.method == 'POST':
            behaviour, enabled = (request.form.get(key) for key in ('behaviour', 'enabled'))
            if behaviour not in {
                'friday', 'morning', 'financing', 'batch_cutoff', 'financial_distress',
                'reminder', 'missing_po', 'receiving_mismatch', 'wrong_recipient',
                'partial_payment', 'higher_credit_limit',
            } or enabled not in {'true', 'false'}:
                abort(400)
            args = (behaviour, enabled == 'true')
        try:
            return jsonify(coordinator().coffee_payment_windows(*args))
        except CoordinatorError:
            logging.exception('CoffeeBench payment windows unavailable')
            return jsonify(error='CoffeeBench payment windows could not be confirmed.'), 502

    @app.post('/api/company/background/<provider_id>/control')
    def control_background_provider(provider_id):
        if provider_id not in {'coffeebench','ecommercebench'}: abort(404)
        raw=request.form.get('enabled')
        key=request.headers.get('Idempotency-Key') or request.form.get('idempotency_key')
        if raw not in {'true','false'} or not key: abort(400)
        try:
            result=coordinator().control_background_provider(provider_id,raw=='true',idempotency_key=key)
            if (result.get('provider_id')!=provider_id or not isinstance(result.get('enabled'),bool)
                    or not isinstance(result.get('in_flight'),bool)
                    or result.get('status') not in {'stopped','stopping','starting','active','error'}):
                raise CoordinatorError(f'company/background/{provider_id}/control','invalid_response')
            return jsonify({key:result[key] for key in ('provider_id','enabled','in_flight','status')})
        except Exception as error:
            logging.exception('%s background control failed',provider_id)
            message=f'{provider_id} background control could not be confirmed ({type(error).__name__}). Refresh to reconcile the requested state.'
            save_company_error(message)
            return jsonify(error=message),502
    @app.post('/api/company/clock/speed')
    def set_company_clock_speed():
        raw=request.form.get('speed')
        key=request.headers.get('Idempotency-Key') or request.form.get('idempotency_key')
        if raw is None or not key: abort(400)
        try: speed=float(raw)
        except ValueError: abort(400)
        if not math.isfinite(speed) or speed<=0: abort(400)
        try:
            payload=coordinator().set_company_clock_speed(speed,idempotency_key=key)
            state=safe_company(payload,require_activity=True)
            confirmed_speed=state.get('clock_speed')
            if confirmed_speed is None:
                raise CoordinatorError('company/clock/speed','missing_clock_speed')
            notify()
            return jsonify(clock_speed=confirmed_speed)
        except Exception as error:
            logging.exception('Company clock speed update failed')
            return jsonify(error=f'Company clock speed could not be confirmed ({type(error).__name__}). Refresh to retry.'),502
    def activity_participant_names():
        profile = json.loads(Path(app.config['COMPANY_PROFILE_FILE']).read_text(encoding='utf-8'))
        company = profile['company']
        names = {'viv': 'Viv', 'customer': 'Cornerstone Customer'}
        for employee in profile['employees']:
            name = employee['displayName']
            employee_id = employee['id']
            names[employee_id] = names['employee-' + employee_id] = name
            names[employee['email'].casefold()] = name
            username = employee.get('accounts', {}).get('rocketchat', {}).get('username')
            if username:
                names[username.casefold()] = name
        support_name = names.get(company.get('supportEmployeeId'))
        if support_name:
            names[company.get('supportActorId', 'requester')] = support_name
        if company.get('assistantEmail'):
            names[company['assistantEmail'].casefold()] = 'Viv'
        for party in company.get('background', {}).get('coffee', {}).get('counterparties', {}).values():
            names[party['actor_id'].casefold()] = party['display_name']
            names[party['email'].casefold()] = party['display_name']
        return names

    @app.post('/api/activity/resend')
    def api_resend_activity():
        data = request.get_json()
        try:
            return jsonify(coordinator().resend_company_handoff(
                data.get('message_id'), data.get('request_id')))
        except CoordinatorError as error:
            return company_pack_error(error)

    @app.get('/api/activity')
    def api_activity():
        activity=[]
        external_to_local={}
        tracked_runs=all_runs()
        for item in tracked_runs:
            run=row(item['id'])
            if run.get('simulation_run_id'):
                external_to_local[run['simulation_run_id']]=run['id']
            for index,message in enumerate(run['messages']):
                author=message.get('author')
                incoming=bool(author and author==getattr(client(),'viv_username',None))
                content=message.get('text','')
                activity.append({'id':f"message:{run['id']}:{index}",'type':'message',
                    'timestamp':message.get('timestamp'),'title':run['title'],
                    'summary':content[:240],'content':content,'actor':author,
                    'direction':'received' if incoming else 'sent','run_id':run['id']})
        company_error=None
        try:
            company=company_state()
            for entry in company['activity']:
                title=entry.get('title') or entry.get('event','Company update')
                content=entry.get('content') if entry.get('type') == 'email' else (entry.get('content') or entry.get('description',''))
                event_type=entry.get('type')
                if event_type not in {'email','message','chat','chatter','action'}: event_type='action'
                activity.append({'id':entry['id'],'type':event_type,
                    'timestamp':entry['timestamp'],'title':title,
                    'summary':entry.get('summary') or (content or '')[:240],
                    'content':content,'actor':entry.get('actor'),
                    'recipient':entry.get('recipient'), 'related_activity_id':entry.get('related_activity_id'),
                    'delivery_status':entry.get('delivery_status'), 'attachments':entry.get('attachments'),
                    'native_document':entry.get('native_document'),
                    'direction':entry.get('direction'), 'can_resend':entry.get('can_resend'),
                    'run_id':external_to_local.get(entry.get('experiment_id')),
                    **({'coffee_update':entry['coffee_update']} if 'coffee_update' in entry else {})})
        except Exception:
            logging.exception('Company activity retrieval failed')
            company_error='Company activity could not be retrieved.'
        # A run's outbox and the evidence collector can record the same email.
        # Keep separate send and receipt observations, and combine duplicate metadata.
        unique_activity = {}
        for item in activity:
            identity = (item['type'], item['id'], item.get('direction'))
            if identity not in unique_activity:
                unique_activity[identity] = item
            else:
                record = unique_activity[identity]
                for field, value in item.items():
                    if not record.get(field) and value:
                        record[field] = value
        activity = list(unique_activity.values())
        metrics={
            'emails_sent': None if company_error else sum(item['type']=='email' and item.get('direction')=='sent' for item in activity),
            'emails_received': None if company_error else sum(item['type']=='email' and item.get('direction')=='received' for item in activity),
            'messages': sum(item['type'] in {'message', 'chat'} for item in activity),
            'open_experiments': None if company_error else (
                sum(item['kind']=='workplace' and item['status'] not in {'completed','failed'} for item in tracked_runs)
                + sum(item['status'] not in {'completed','failed'} for item in company['experiments'])),
        }
        def category(item):
            return 'message' if item['type'] == 'chat' else item['type']

        def chatter_message(item):
            return category(item) == 'message' and item.get('direction') == 'sent' and bool(item.get('actor'))

        counts = {'all': len(activity)}
        for item in activity:
            name = category(item)
            counts[name] = counts.get(name, 0) + 1
            if chatter_message(item):
                counts['chatter'] = counts.get('chatter', 0) + 1

        def activity_rank(item):
            try:
                timestamp = datetime.fromisoformat(item.get('timestamp') or '')
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                date = timestamp.astimezone(timezone.utc).isoformat(timespec='microseconds')
            except ValueError:
                date = ''
            return date, item['type'], item['id'], item.get('direction') or ''

        filter_type = request.args.get('type', 'all')
        if filter_type not in {'all', 'email', 'message', 'chatter', 'action'}:
            abort(400)
        cursor = request.args.get('before')
        if cursor is not None:
            try:
                rank = json.loads(cursor)
            except ValueError:
                abort(400)
            if not isinstance(rank, list) or len(rank) != 4 or not all(isinstance(value, str) for value in rank):
                abort(400)
        before = tuple(rank) if cursor is not None else None
        page = nlargest(11, (
            item for item in activity
            if (filter_type == 'all' or category(item) == filter_type
                or (filter_type == 'chatter' and chatter_message(item)))
            and (before is None or activity_rank(item) < before)
        ), key=activity_rank)
        activity = page[:10]
        next_cursor = json.dumps(activity_rank(activity[-1])) if len(page) > 10 else None
        try:
            participant_names = activity_participant_names()
        except (OSError, ValueError, KeyError, TypeError):
            logging.exception('Activity participant names could not be loaded')
            participant_names = {'viv': 'Viv'}
        for item in activity:
            for field in ('actor', 'recipient'):
                participant = item.get(field)
                if participant:
                    item[field + '_name'] = participant_names.get(participant.casefold(), participant)
        response = {'activity': activity, 'metrics': metrics, 'counts': counts, 'next_cursor': next_cursor}
        if company_error: response['error']=company_error
        return jsonify(response)
    @app.get('/experiments/<experiment_id>')
    def experiment_detail(experiment_id):
        return frontend()
    @app.get('/experiments/<experiment_id>/inputs/<int:index>')
    def experiment_input(experiment_id,index):
        if experiment_id not in experiments: abort(404)
        files=inputs.get(experiment_id,[])
        if index>=len(files): abort(404)
        return send_file(Path(app.config['REPO'])/'workspaces/tasks'/experiment_id/files[index]['source'],as_attachment=True)
    @app.post('/experiments/<experiment_id>/runs')
    def start_run(experiment_id):
        e=definition_for(experiment_id)
        key=request.form.get('idempotency_key')
        if not key: abort(400)
        run_id=str(uuid.uuid4())
        with db() as c:
            inserted=c.execute('''INSERT OR IGNORE INTO runs(id,experiment_id,title,description,status,
                created_at,idempotency_key,kind,company_id) VALUES(?,?,?,?,?,?,?,?,?)''',
                (run_id,e['id'],e['title'],e['description'],'queued',datetime.now(timezone.utc).isoformat(),
                 key,e.get('kind','workplace'),'company' if e.get('kind')=='simulation' else None)).rowcount
            existing=c.execute('SELECT id,experiment_id FROM runs WHERE idempotency_key=?',(key,)).fetchone()
            if existing['experiment_id']!=e['id']: abort(409)
            existing=existing['id']
        if inserted: executor.submit(dispatch,run_id);notify()
        return run_response(existing)
    @app.get('/runs')
    def runs(): return frontend()
    @app.get('/runs/<run_id>')
    def run_detail(run_id):
        return frontend()
    @app.get('/api/runs/<run_id>')
    def api_run(run_id):
        item=row(run_id)
        if item['kind']=='simulation' and item['simulation_run_id']:
            try: refresh_simulation(run_id)
            except Exception as error:
                logging.exception('Simulation status refresh failed run=%s',run_id)
                update(run_id,error=f'Simulation status could not be retrieved ({type(error).__name__}). Refresh to retry.')
            item=row(run_id)
        return jsonify(run=item)

    def evaluation_run(run_id):
        item=row(run_id)
        if item['kind']!='simulation' or not item.get('simulation_run_id'):
            return item, None
        return item, item['simulation_run_id']

    def safe_evaluation(value):
        """Allowlist the public score contract; never proxy evaluator internals."""
        if not isinstance(value,dict):
            raise CoordinatorError('evaluation','invalid_response')
        allowed={
            'evaluation_id','company_id','experiment_id','run_id','generation',
            'benchmark','domain','benchmark_version','evaluation_mode','evaluated_at',
            'requested_at','evidence_ref','evidence_hash','status','scores','criterion_results',
            'warnings','mapping_gaps','native_scale','correlation_id','failure','lifecycle',
        }
        result={key:value[key] for key in allowed if key in value}
        def safe_code(code):
            return isinstance(code,str) and bool(re.fullmatch(r'[A-Za-z0-9_.:-]{1,96}',code))
        required=('evaluation_id','run_id','status')
        if any(not isinstance(result.get(key),str) for key in required):
            raise CoordinatorError('evaluation','invalid_response')
        if result['status'] not in {'completed','incomplete','failed'}:
            raise CoordinatorError('evaluation','invalid_status')
        if result.get('evaluation_mode') not in (None,'native_benchmark','company_adapted'):
            raise CoordinatorError('evaluation','invalid_mode')
        if result.get('evaluation_mode') is None and result['status'] != 'failed':
            raise CoordinatorError('evaluation','missing_mode')
        lifecycle=result.get('lifecycle')
        if lifecycle is not None and lifecycle not in {'reserved','running','uncertain'}:
            raise CoordinatorError('evaluation','invalid_lifecycle')
        if lifecycle is None:
            result.pop('lifecycle',None)
        if 'scores' in result:
            scores=result['scores']
            if not isinstance(scores,dict) or any(
                not isinstance(key,str) or score is not None and not isinstance(score,(int,float,str,bool))
                for key,score in scores.items()
            ):
                raise CoordinatorError('evaluation','invalid_scores')
        if 'criterion_results' in result:
            criteria=result['criterion_results']
            if not isinstance(criteria,list) or any(not isinstance(item,dict) for item in criteria):
                raise CoordinatorError('evaluation','invalid_criteria')
            safe_criteria=[]
            for item in criteria:
                safe_criteria.append({key:item[key] for key in (
                    'criterion_id','criterion','name','status','score','value','actual',
                    'expected','maximum','max_score','weight','gap_code','passed'
                ) if key in item and (item[key] is None or isinstance(item[key],(str,int,float,bool)))})
            result['criterion_results']=safe_criteria
        for field in ('warnings','mapping_gaps'):
            if field in result:
                if not isinstance(result[field],list) or any(not safe_code(item) for item in result[field]):
                    raise CoordinatorError('evaluation',f'invalid_{field}')
        if 'correlation_id' in result and not safe_code(result['correlation_id']):
            raise CoordinatorError('evaluation','invalid_correlation_id')
        if 'failure' in result and result['failure'] is not None:
            failure=result['failure']
            if not isinstance(failure,dict):
                raise CoordinatorError('evaluation','invalid_failure')
            result['failure']={key:failure[key] for key in ('code','exception_type')
                               if safe_code(failure.get(key))}
        if 'native_scale' in result:
            scale=result['native_scale']
            allowed_scales={'boolean','categorical','seconds','native_reward_0_to_1','count','native_metric'}
            if not isinstance(scale,dict) or any(
                not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',key)
                or not isinstance(value,str) or value not in allowed_scales
                for key,value in scale.items()
            ):
                raise CoordinatorError('evaluation','invalid_native_scale')
        return result

    @app.get('/api/runs/<run_id>/evaluations')
    def api_run_evaluations(run_id):
        item,coordinator_run_id=evaluation_run(run_id)
        if coordinator_run_id is None:
            return jsonify(evaluations=[],not_evaluated=True)
        payload=(coordinator().provider_evaluations(coordinator_run_id)
                 if item['experiment_id'].startswith('tau-provider-') else coordinator().evaluations(coordinator_run_id))
        evaluations=payload.get('evaluations')
        if not isinstance(evaluations,list):
            raise CoordinatorError('evaluations','invalid_response')
        safe=[safe_evaluation(evaluation) for evaluation in evaluations]
        if any(evaluation.get('run_id')!=coordinator_run_id for evaluation in safe):
            raise CoordinatorError('evaluations','scope_mismatch')
        return jsonify(evaluations=safe,not_evaluated=not bool(safe))

    @app.get('/api/runs/<run_id>/evaluations/latest')
    def api_latest_evaluation(run_id):
        item,coordinator_run_id=evaluation_run(run_id)
        if coordinator_run_id is None:
            return jsonify(evaluation=None,not_evaluated=True)
        try:
            payload=(coordinator().latest_provider_evaluation(coordinator_run_id)
                     if item['experiment_id'].startswith('tau-provider-') else coordinator().latest_evaluation(coordinator_run_id))
        except CoordinatorError as error:
            if error.code == 'http_404':
                return jsonify(evaluation=None,not_evaluated=True)
            raise
        evaluation=payload.get('evaluation')
        if evaluation is None:
            return jsonify(evaluation=None,not_evaluated=True)
        safe=safe_evaluation(evaluation)
        if safe.get('run_id')!=coordinator_run_id:
            raise CoordinatorError('evaluation','scope_mismatch')
        return jsonify(evaluation=safe,not_evaluated=False)

    @app.get('/api/runs/<run_id>/evaluations/<evaluation_id>')
    def api_evaluation_detail(run_id,evaluation_id):
        item,coordinator_run_id=evaluation_run(run_id)
        if coordinator_run_id is None:
            abort(404)
        try:
            payload=(coordinator().provider_evaluation(coordinator_run_id,evaluation_id)
                     if item['experiment_id'].startswith('tau-provider-') else coordinator().evaluation(evaluation_id))
        except CoordinatorError as error:
            if error.code == 'http_404':
                abort(404)
            raise
        evaluation=payload.get('evaluation')
        if not isinstance(evaluation,dict):
            raise CoordinatorError('evaluation','invalid_response')
        safe=safe_evaluation(evaluation)
        if safe.get('run_id')!=coordinator_run_id:
            abort(404)
        return jsonify(evaluation=safe)

    @app.post('/runs/<run_id>/evaluate')
    def evaluate_run(run_id):
        item,coordinator_run_id=evaluation_run(run_id)
        if coordinator_run_id is None:
            abort(409)
        key=request.form.get('idempotency_key')
        if not key:
            abort(400)
        payload=(coordinator().evaluate_provider(coordinator_run_id,idempotency_key=key)
                 if item['experiment_id'].startswith('tau-provider-') else coordinator().evaluate(coordinator_run_id,idempotency_key=key))
        evaluation=payload.get('evaluation')
        if not isinstance(evaluation,dict):
            raise CoordinatorError('evaluation','invalid_response')
        safe=safe_evaluation(evaluation)
        if safe.get('run_id')!=coordinator_run_id:
            raise CoordinatorError('evaluation','scope_mismatch')
        return jsonify(evaluation=safe)
    @app.post('/runs/<run_id>/refresh')
    def refresh_run(run_id):
        item=row(run_id)
        if item['kind']=='simulation':
            try: refresh_simulation(run_id)
            except Exception as error:
                logging.exception('Simulation status refresh failed run=%s',run_id)
                update(run_id,error=f'Simulation status could not be retrieved ({type(error).__name__}). Refresh to retry.')
        else:
            try: refresh(run_id)
            except Exception:
                logging.exception('Manual thread refresh failed run=%s',run_id)
                update(run_id,error='Chat updates could not be retrieved. Refresh to retry.')
        return run_response(run_id)
    @app.post('/runs/<run_id>/retry')
    def retry_run(run_id):
        with db() as c:
            claimed=c.execute("UPDATE runs SET status='queued',error=NULL,revision=revision+1 WHERE id=? AND status IN ('failed','uncertain')",(run_id,)).rowcount
        if claimed: executor.submit(dispatch,run_id);notify()
        return run_response(run_id)
    @app.post('/api/chat-events')
    def chat_events():
        data=request.get_json()
        expected=os.environ['EXPERIMENTS_WEBHOOK_TOKEN']
        if not isinstance(data,dict): abort(400)
        if not expected: raise RuntimeError('EXPERIMENTS_WEBHOOK_TOKEN must be nonempty')
        if not secrets.compare_digest(str(data.get('token','')),expected): abort(403)
        room=data.get('channel_id') or data.get('rid')
        executor.submit(refresh_channel,room)
        return jsonify(success=True)
    @app.get('/api/health')
    def health():
        with db() as c: c.execute('SELECT 1').fetchone()
        return jsonify(status='ok')
    @app.get('/api/runs')
    def api_runs():
        result=[]
        for current in all_runs():
            if current['kind']=='simulation' and current['simulation_run_id']:
                try: refresh_simulation(current['id'])
                except Exception as error:
                    logging.exception('Simulation status refresh failed run=%s',current['id'])
                    update(current['id'],error=f'Simulation status could not be retrieved ({type(error).__name__}). Refresh to retry.')
            result.append(row(current['id']))
        return jsonify(runs=result)
    @app.get('/api/events')
    def events():
        def stream():
            last=-1
            while True:
                with changed:
                    received=changed.wait_for(lambda: version[0]!=last,
                        timeout=app.config['EVENTS_WAIT_SECONDS'] * _kernel_virtual_time_rate())
                    last=version[0]
                if received:
                    snapshot=[{k:r[k] for k in ('id','status','revision')} for r in all_runs()]
                    yield 'event: runs\ndata: '+json.dumps({'runs':snapshot})+'\n\n'
                    yield 'event: company\ndata: {}\n\n'
                    yield 'event: activity\ndata: {}\n\n'
                else:
                    # Read-only refresh lets upstream coordinator activity reach this stream
                    # even when no local Rocket.Chat webhook or dashboard request arrives.
                    try:
                        company_state()
                        yield 'event: company\ndata: {}\n\n'
                        yield 'event: activity\ndata: {}\n\n'
                    except Exception:
                        logging.exception('Company SSE refresh failed')
                        yield 'event: error\ndata: '+json.dumps({'error':'Company updates are temporarily unavailable.'})+'\n\n'
        return Response(stream(),mimetype='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})
    return app

if __name__=='__main__':
    create_app().run(host='0.0.0.0',port=8110,threaded=True)
