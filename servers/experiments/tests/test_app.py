"""Flask and SQLite integration tests with Rocket.Chat replaced at its boundary."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import unittest
import uuid
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from servers.experiments.app import create_app
from servers.experiments.coordinator import CoordinatorError


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


class FakeRocketChat:
    """Thread-safe fake for the external Rocket.Chat client boundary."""

    viv_username = "viv"

    def __init__(self):
        self._condition = threading.Condition()
        self.dispatches: list[tuple[str, str, str, list[dict]]] = []
        self.thread_calls: list[tuple[str, str]] = []
        self.thread_completions = 0
        self.messages_by_room: dict[str, list[dict[str, str]]] = {}
        self.dispatch_gate = threading.Event()
        self.dispatch_gate.set()
        self.thread_gate = threading.Event()
        self.thread_gate.set()

    def dispatch(self, run_id, title, description, inputs=()):
        with self._condition:
            self.dispatches.append((str(run_id), title, description, list(inputs)))
            self._condition.notify_all()
        if not self.dispatch_gate.wait(timeout=4):
            raise TimeoutError("test dispatch gate was not released")
        return {"room_id": f"room-{run_id}", "message_id": f"exp-{run_id}"}

    def thread(self, room_id, message_id):
        with self._condition:
            self.thread_calls.append((room_id, message_id))
            self._condition.notify_all()
        if not self.thread_gate.wait(timeout=4):
            raise TimeoutError("test thread gate was not released")
        with self._condition:
            result = list(self.messages_by_room.get(room_id, []))
            self.thread_completions += 1
            self._condition.notify_all()
            return result

    def wait_for_dispatches(self, count):
        with self._condition:
            self.assert_eventually(lambda: len(self.dispatches) >= count, "dispatch")

    def wait_for_threads(self, count):
        with self._condition:
            self.assert_eventually(lambda: len(self.thread_calls) >= count, "thread read")

    def wait_for_thread_completions(self, count):
        with self._condition:
            self.assert_eventually(lambda: self.thread_completions >= count, "completed thread read")

    def assert_eventually(self, predicate, operation):
        if not self._condition.wait_for(predicate, timeout=3):
            raise AssertionError(f"timed out waiting for Rocket.Chat {operation}")


class FakeCoordinator:
    """Stateful coordinator boundary fake for company and case-link flows."""

    def __init__(self):
        self._condition=threading.Condition()
        self.state={'company_id':'company','status':'not_started','simulated_time':None,
                    'paused':False,'erp_site_id':None,'experiments':[],'activity':[]}
        self.start_keys=[]
        self.pause_requests=[]
        self.experiment_requests=[]
        self.fail_company_reads=False
        self.fail_pause_calls=0
        self.company_reads=0
        self.experiment_failure=None
        self.evaluation_requests=[]
        self.evaluation_by_key={}
        self.evaluations_by_run={}
        self.evaluation_status='completed'
        self.company_native_evaluation_requests=[]
        self.company_native_evaluations={provider: [] for provider in ('coffeebench','ecommercebench')}
        self.provider_tasks=[]
        self.provider_cases={}
        self.provider_catalog_failure=None

    def provider_catalog(self):
        if self.provider_catalog_failure:
            raise self.provider_catalog_failure
        return {'schema_version':1,'upstream_commit':'c'*40,'tasks':deepcopy(self.provider_tasks)}

    def start_provider_experiment(self, provider_id, task_id, *, idempotency_key):
        with self._condition:
            experiment_id=str(uuid.uuid5(uuid.NAMESPACE_URL,idempotency_key))
            if experiment_id not in self.provider_cases:
                self.provider_cases[experiment_id]={
                    'experiment_id':experiment_id,'provider_id':provider_id,'task_id':task_id,
                    'status':'registered','portal_url':'http://company.example.test/?case='+experiment_id,
                    'activity':[{'status':'registered','occurred_at':'2026-10-30T12:00:00+00:00'}],
                }
                self.experiment_requests.append(('TAU_PROVIDER',idempotency_key,provider_id,task_id))
            self._condition.notify_all()
            return deepcopy(self.provider_cases[experiment_id])

    def provider_experiment(self, experiment_id):
        return {'experiment':deepcopy(self.provider_cases[experiment_id])}

    def company_evaluations(self, provider_id):
        return {'scope':'company_global','evaluations':deepcopy(self.company_native_evaluations[provider_id])}

    def latest_company_evaluation(self, provider_id):
        rows=self.company_native_evaluations[provider_id]
        return {'selection':'latest_saved','evaluation':deepcopy(rows[0]) if rows else None}

    def start_company_evaluation(self, provider_id, *, idempotency_key):
        self.company_native_evaluation_requests.append((provider_id,idempotency_key))
        existing=next((item for item in self.company_native_evaluations[provider_id]
                       if item['evaluation_id']==idempotency_key),None)
        if existing is None:
            existing={
                'evaluation_id':idempotency_key,'company_id':'company','provider_id':provider_id,
                'source_id':'a'*64,'initialization_request_hash':'b'*64,'upstream_commit':'c'*40,
                'seed':17,'started_at':'2026-10-30T00:00:00+00:00','native_company_time':'2026-10-30T12:00:00+00:00',
                'native_state_ref':'checkpoint:coffee:17','native_state_hash':'d'*64,
                'money_metric_units':{'audit.annual.true_net_income':'USD'} if provider_id=='coffeebench' else {'profitability':'GBP'},
                'evaluation_mode':'company_adapted','scope':'company_global','attribution':'company_global_not_experiment',
                'requested_at':'2026-10-30T12:00:00+00:00','evaluated_at':'2026-10-30T12:00:01+00:00',
                'status':'incomplete','scores':{'native_metric':None},'criterion_results':[],
                'warnings':[],'mapping_gaps':[],'native_scale':{'native_metric':'native_metric'},
                'evidence_ref':'evaluation:test:snapshot','evidence_hash':'e'*64,
                'correlation_id':'corr-test','failure':None,'lifecycle':None,
                'prompt':'must-not-leak','evidence_snapshot':{'secret':'must-not-leak'},
            }
            self.company_native_evaluations[provider_id].insert(0,existing)
        return {'evaluation':deepcopy(existing)}

    def company(self):
        with self._condition:
            self.company_reads+=1
            self._condition.notify_all()
            if self.fail_company_reads: return {'company_id':'company','status':'running','paused':False,
                                                 'experiments':[{'scenario_id':'A1'}],'activity':[]}
            return deepcopy(self.state)

    def wait_for_company_reads(self,count):
        with self._condition:
            if not self._condition.wait_for(lambda:self.company_reads>=count,timeout=3):
                raise AssertionError('timed out waiting for read-only company refresh')

    def start_company(self, *, idempotency_key):
        with self._condition:
            self.start_keys.append(idempotency_key)
            self.state.update(status='running',simulated_time='2026-10-30T09:00:00+00:00',
                              erp_site_id='company-site')
            self._condition.notify_all()
            return deepcopy(self.state)

    def pause_company(self, paused, *, idempotency_key):
        with self._condition:
            self.pause_requests.append((paused,idempotency_key))
            if self.fail_pause_calls:
                self.fail_pause_calls-=1
                raise RuntimeError('mocked pause timeout')
            self.state.update(paused=paused,status='paused' if paused else 'running')
            self._condition.notify_all()
            return deepcopy(self.state)

    def control_background_provider(self, provider_id, enabled, *, idempotency_key):
        self.background_control_requests=getattr(self,'background_control_requests',[])
        self.background_control_requests.append((provider_id,enabled,idempotency_key))
        background=self.state.setdefault('background',{'initialized':True,'providers':{},'errors':{}})
        background['providers'][provider_id]={
            'status':'active' if enabled else 'stopped','enabled':enabled,'in_flight':False,
            'initialized':True,'next_due':None,
        }
        return {'provider_id':provider_id,'enabled':enabled,'in_flight':False,
                'status':'active' if enabled else 'stopped'}

    def start_experiment(self, scenario_id, *, idempotency_key, task_id=None):
        with self._condition:
            self.experiment_requests.append((scenario_id,idempotency_key))
            if self.state['status'] not in {'running','paused'}:
                raise CoordinatorError('company/experiments','http_409')
            experiment_id=str(uuid.uuid5(uuid.NAMESPACE_URL,idempotency_key))
            entry={'experiment_id':experiment_id,'scenario_id':scenario_id,'task_id':task_id,'agent_id':'viv',
                   'generation':0,'status':'running','simulated_time':self.state['simulated_time'],
                   'paused':self.state['paused']}
            if not any(item['experiment_id']==experiment_id for item in self.state['experiments']):
                self.state['experiments'].append(entry)
                self.state['activity'].append({'id':f'activity-{experiment_id}','timestamp':self.state['simulated_time'],
                    'event':'case_opened','description':'A new company case was opened.',
                    'type':'action','title':'Case opened','summary':'Case opened',
                    'experiment_id':experiment_id,'scenario_id':scenario_id,
                    'private_record':{'expected_answer':'secret'}})
            failure=self.experiment_failure
            self.experiment_failure=None
            self._condition.notify_all()
            if failure is not None: raise failure
            return {'experiment_id':experiment_id,'scenario_id':scenario_id,'status':'running',
                    'generation':0,'simulated_time':self.state['simulated_time'],'paused':self.state['paused']}

    def start_evaluation(self, run_id, *, idempotency_key):
        with self._condition:
            self.evaluation_requests.append((run_id,idempotency_key))
            if idempotency_key in self.evaluation_by_key:
                return {'evaluation':deepcopy(self.evaluation_by_key[idempotency_key])}
            evaluation_id=str(uuid.uuid5(uuid.NAMESPACE_URL,idempotency_key))
            evaluation={
                'evaluation_id':evaluation_id,'company_id':'company','experiment_id':run_id,
                'run_id':run_id,'generation':0,'benchmark':'CoffeeBench','domain':'supplier_dispute',
                'benchmark_version':'commit-1',
                'evaluation_mode':None if self.evaluation_status=='failed' else 'company_adapted',
                'evaluated_at':None if self.evaluation_status=='incomplete' else '2026-10-30T12:00:00+00:00',
                'requested_at':'2026-10-30T11:59:00+00:00','evidence_ref':'report:'+evaluation_id,
                'evidence_hash':'a'*64,'status':self.evaluation_status,
                'scores':({'reward':None} if self.evaluation_status=='incomplete'
                          else {} if self.evaluation_status=='failed'
                          else {'reward':0.5,'complete':False}),
                'criterion_results':[{'criterion_id':'invoice_match','status':'not_met','actual':1,'expected':2,
                                      'gap_code':'quantity_mismatch','private_evidence':'must-not-leak'}],
                'warnings':['shared_company_state'],'mapping_gaps':['unmapped_payment'],
                'native_scale':{'reward':'native_reward_0_to_1','complete':'boolean'},
                'correlation_id':'30b95e99-1460-4b99-bb45-726bbfab517a',
                'failure':{'code':'benchmark_unavailable','exception_type':'RuntimeError'} if self.evaluation_status=='failed' else None,
                'lifecycle':'running' if self.evaluation_status=='incomplete' else None,
                'prompt':'must-not-leak',
                'evidence_snapshot':{'private':'must-not-leak'},
            }
            self.evaluation_by_key[idempotency_key]=evaluation
            self.evaluations_by_run.setdefault(run_id,[]).insert(0,evaluation)
            return {'evaluation':deepcopy(evaluation)}

    def evaluate(self, run_id, *, idempotency_key):
        return self.start_evaluation(run_id,idempotency_key=idempotency_key)

    def evaluations(self, run_id):
        with self._condition:
            return {'evaluations':deepcopy(self.evaluations_by_run.get(run_id,[]))}

    def latest_evaluation(self, run_id):
        with self._condition:
            rows=self.evaluations_by_run.get(run_id,[])
            return {'evaluation':deepcopy(rows[0]) if rows else None}

    def evaluation(self, evaluation_id):
        with self._condition:
            for rows in self.evaluations_by_run.values():
                for evaluation in rows:
                    if evaluation['evaluation_id']==evaluation_id:
                        return {'evaluation':deepcopy(evaluation)}
            return {'evaluation':None}

    def wait_for_experiments(self, count):
        with self._condition:
            if not self._condition.wait_for(lambda:len(self.experiment_requests)>=count,timeout=3):
                raise AssertionError('timed out waiting for coordinator experiment dispatch')

class ExperimentAppIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "experiments.sqlite"
        self.operator_key_file = Path(self.temp_dir.name) / "operator-key"
        self.operator_key_file.write_text("integration-test-operator-key\n")
        self.operator_key_file.chmod(0o600)
        self.rocket = FakeRocketChat()
        self.coordinator = FakeCoordinator()
        self.apps=[]
        self.app = self.make_app()
        self.client = self.app.test_client()
        self.authenticate(self.client)

    def tearDown(self):
        self.rocket.dispatch_gate.set()
        self.rocket.thread_gate.set()
        for app in self.apps:
            app.extensions['experiments_executor'].shutdown(wait=True)
        self.temp_dir.cleanup()

    def make_app(self):
        app=create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "integration-test-secret",
                "DATABASE": str(self.database),
                "REPO": str(REPOSITORY_ROOT),
                "CHAT_PUBLIC_URL": "https://chat.example.test",
                "OPERATOR_KEY_FILE": str(self.operator_key_file),
                "SESSION_COOKIE_SECURE": False,
            },
            client_factory=lambda: self.rocket,
            coordinator_factory=lambda: self.coordinator,
        )
        self.apps.append(app)
        return app

    def authenticate(self, client):
        status = client.get('/api/operator/status')
        self.assertEqual(status.status_code, 200)
        if status.get_json()['authenticated']:
            return
        csrf = client.get('/api/session').get_json()['csrf_token']
        response = client.post('/api/operator/login', data={
            'csrf_token': csrf, 'key': self.operator_key_file.read_text().strip(),
        })
        self.assertEqual(response.status_code, 200)

    def test_activity_and_library_direct_links_serve_the_frontend(self):
        static = Path(self.temp_dir.name) / "static"
        (static / "app").mkdir(parents=True)
        (static / "app/index.html").write_text("<main>company dashboard</main>")
        self.app.static_folder = str(static)
        for path in ("/", "/experiments", "/coverage", "/runs"):
            with self.subTest(path=path):
                with self.client.get(path) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(b"company dashboard", response.data)

    def test_retail_catalog_dispatches_original_task_id_through_shared_coordinator(self):
        self.coordinator.state.update(status='running', simulated_time='2026-10-30T09:00:00+00:00')
        library = self.client.get('/api/catalog').get_json()['experiments']
        entries = [item for item in library if item.get('scenario_id') == 'TAU_RETAIL']
        self.assertEqual(len(entries), 114)
        entry = next(item for item in entries if item['task_id'] == '0')
        self.assertNotIn('evaluation_criteria', entry)
        response = self.start_run(entry['id'], 'native-retail-task-zero')
        self.assertEqual(response.status_code, 303)
        self.coordinator.wait_for_experiments(1)
        self.assertEqual(self.coordinator.experiment_requests[0][0], 'TAU_RETAIL')
        self.assertEqual(self.coordinator.state['experiments'][0]['task_id'], '0')
        self.assertEqual(self.rocket.dispatches, [])

    def test_provider_library_run_is_tracked_without_chat_and_retries_keep_one_case(self):
        self.coordinator.state.update(status='running',simulated_time='2026-10-30T09:00:00+00:00')
        self.coordinator.provider_tasks=[{
            'provider_id':'tau2-banking','task_id':'task /001.£',
            'classification':'financial',
            'description':'Investigate an unexpected account charge. Ask the bank to explain the transaction.',
            'role_fit':'mixed_customer','role_fit_note':'The request spans personal and business accounts, cards, or financial activity.',
            'evaluation_criteria':{'private':'must-not-leak'},
        }]
        payload=self.client.get('/api/catalog').get_json()
        entry=next(item for item in payload['experiments'] if item.get('provider_id')=='tau2-banking')
        self.assertEqual(entry['title'],'Investigate an unexpected account charge. Ask the bank to explain the transaction.')
        self.assertEqual(entry['dependencies'],['Email','Banking portal'])
        self.assertEqual(entry['role_fit'],'mixed_customer')
        self.assertEqual(entry['role_fit_note'],
                         'The request spans personal and business accounts, cards, or financial activity.')
        self.assertNotIn('must-not-leak',json.dumps(entry))
        first=self.start_run(entry['id'],'provider-case-launch')
        local_id=first.location.rsplit('/',1)[-1]
        self.coordinator.wait_for_experiments(1)
        self.wait_for_sse_status(local_id,'running')
        second=self.start_run(entry['id'],'provider-case-launch')
        self.assertEqual(first.location,second.location)
        record=self.client.get('/api/runs/'+local_id).get_json()['run']
        self.assertEqual(record['simulation_run_id'],next(iter(self.coordinator.provider_cases)))
        self.assertEqual(record['activity'][0]['description'],'The company request was delivered.')
        self.assertEqual(record['provider_url'],'http://company.example.test/?case='+record['simulation_run_id'])
        self.assertEqual(len(self.coordinator.provider_cases),1)
        self.assertEqual(self.coordinator.experiment_requests[0][2:],('tau2-banking','task /001.£'))
        self.assertEqual(self.rocket.dispatches,[])

    def test_native_optional_classification_does_not_hide_provider_catalogue(self):
        from servers.simulation.provider_contracts import ProviderTaskCatalogEntry

        # Native Banking and Airline entries omit this optional Telecom metadata.
        self.coordinator.provider_tasks = [ProviderTaskCatalogEntry(
            provider_id=provider, task_id='source-fixture', description='Review the customer request.'
        ).model_dump(mode='json') for provider in ('tau2-banking', 'tau2-airline')]
        response = self.client.get('/api/catalog')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIsNone(payload['provider_catalog_error'])
        providers = [item for item in payload['experiments'] if item.get('provider_id')]
        self.assertEqual({item['provider_id'] for item in providers}, {'tau2-banking', 'tau2-airline'})
        self.assertTrue(all('classification' not in item for item in providers))
        self.assertEqual(next(item for item in providers if item['provider_id']=='tau2-banking')['role'], 'Finance')

    def test_provider_catalogue_failure_is_visible_without_hiding_existing_assignments(self):
        self.coordinator.provider_catalog_failure=CoordinatorError('provider-catalog','http_503','corr-test')
        response=self.client.get('/api/catalog')
        self.assertEqual(response.status_code,200)
        payload=response.get_json()
        self.assertIn('A1',{entry['id'] for entry in payload['experiments']})
        self.assertEqual(payload['provider_catalog_error'],{'code':'http_503','correlation_id':'corr-test'})

    def test_compatibility_api_exposes_curated_fit_and_catalogue_counts_only(self):
        response = self.client.get('/api/compatibility')
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertEqual(set(result), {
            'schema_version', 'company_persona', 'library_inventory', 'tau_domains', 'source_revisions'
        })
        self.assertEqual(result['schema_version'], 1)
        self.assertIn('industrial manufacturer/distributor', result['company_persona'])
        domains = {item['name']: item for item in result['tau_domains']}
        self.assertEqual(set(domains['Retail']), {
            'name', 'catalogue_count', 'default_base_count', 'fit', 'summary', 'implementation',
            'source_integrity_issue_count'
        })
        self.assertEqual(domains['Retail']['catalogue_count'], 114)
        self.assertEqual(domains['Retail']['source_integrity_issue_count'], 8)
        self.assertEqual(domains['Airline']['fit'], 'provider_persona_for_unchanged_tasks')
        self.assertEqual(domains['Telecom']['financial_task_count'], 1128)
        self.assertEqual(domains['Telecom']['remaining_carrier_state_task_count'], 1157)
        self.assertEqual(domains['Telecom']['default_base_count'], 114)
        self.assertEqual(domains['Intake']['catalogue_count'], 200)
        self.assertEqual(domains['Mock']['fit'], 'internal_test_fixtures')
        inventories = {item['name']: item for item in result['library_inventory']}
        self.assertEqual(inventories['Current workplace library']['count'], 105)
        self.assertIn('21 configured seed runs', inventories['CoffeeBench']['summary'])
        self.assertIn('365-day', inventories['E-CommerceBench']['summary'])
        serialized = json.dumps(result).lower()
        for private_field in ('evaluation_criteria', 'action_arguments', 'original_db', 'expected_answer'):
            self.assertNotIn(private_field, serialized)

    def test_company_native_evaluation_history_is_private_company_scoped_and_preserves_native_values(self):
        anonymous=self.app.test_client()
        before=len(self.coordinator.company_native_evaluation_requests)
        self.assertEqual(anonymous.get('/api/company/evaluations').status_code,401)
        self.assertEqual(anonymous.post('/api/company/evaluations/coffeebench').status_code,401)
        self.assertEqual(len(self.coordinator.company_native_evaluation_requests),before)

        posted=self.client.post('/api/company/evaluations/coffeebench',data={
            'csrf_token':self.csrf_token(), 'idempotency_key':'native-eval-key-1',
        })
        self.assertEqual(posted.status_code,200)
        saved=posted.get_json()['evaluation']
        self.assertEqual(saved['status'],'incomplete')
        self.assertEqual(saved['scores'],{'native_metric':None})
        self.assertEqual(saved['money_metric_units'],{'audit.annual.true_net_income':'USD'})
        self.assertEqual(saved['scope'],'company_global')
        self.assertNotIn('run_id',saved)
        self.assertNotIn('prompt',saved)
        self.assertNotIn('evidence_snapshot',saved)
        self.assertEqual(self.coordinator.company_native_evaluation_requests,[('coffeebench','native-eval-key-1')])

        history=self.client.get('/api/company/evaluations').get_json()
        self.assertEqual(history['scope'],'company_global')
        self.assertEqual(history['attribution'],'company_global_not_experiment')
        provider=history['providers']['coffeebench']
        self.assertEqual(provider['selection'],'latest_saved')
        self.assertEqual(provider['latest']['evaluation_id'],'native-eval-key-1')
        self.assertEqual(len(provider['evaluations']),1)
        self.assertIsNone(history['providers']['ecommercebench']['latest'])

    def test_company_native_evaluation_routes_validate_provider_and_csrf(self):
        self.assertEqual(self.client.post('/api/company/evaluations/not-a-provider',data={
            'csrf_token':self.csrf_token(), 'idempotency_key':'x',
        }).status_code,404)
        self.assertEqual(self.client.post('/api/company/evaluations/coffeebench',data={
            'csrf_token':'wrong', 'idempotency_key':'x',
        }).status_code,400)

    def test_company_history_accepts_failed_evaluation_without_resolved_source_identity(self):
        self.coordinator.company_native_evaluations['coffeebench'] = [{
            'evaluation_id':'offline-evaluation-id', 'company_id':'company',
            'provider_id':'coffeebench', 'upstream_commit':'9e2f3950b5812c8753de81ede9c68babc024b5c8',
            'evaluation_mode':None, 'scope':'company_global',
            'attribution':'company_global_not_experiment', 'requested_at':'2026-10-30T12:00:00+00:00',
            'evaluated_at':'2026-10-30T12:00:01+00:00', 'status':'failed',
            'scores':{}, 'criterion_results':[], 'warnings':[], 'mapping_gaps':[],
            'native_scale':{}, 'failure':{'code':'worker_transport_uncertain','exception_type':'BackgroundWorkerError'},
            'correlation_id':'corr-offline',
        }]

        response=self.client.get('/api/company/evaluations')
        self.assertEqual(response.status_code,200)
        evaluation=response.get_json()['providers']['coffeebench']['latest']
        self.assertEqual(evaluation['evaluation_id'],'offline-evaluation-id')
        self.assertEqual(evaluation['status'],'failed')
        self.assertIsNone(evaluation['evaluation_mode'])
        self.assertEqual(evaluation['failure']['code'],'worker_transport_uncertain')
        self.assertNotIn('source_id',evaluation)
        self.assertNotIn('seed',evaluation)
        self.assertNotIn('started_at',evaluation)

    def test_company_name_is_read_from_identity_json(self):
        root = Path(self.temp_dir.name) / "company-source"
        path = root / "deploy/single-host/company-identities.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"company": {"name": "Configured company"}}))
        self.app.config["REPO"] = str(root)
        self.assertEqual(self.client.get('/api/company').get_json()['company']['name'], "Configured company")
        path.write_text(json.dumps({"company": {"name": "Updated company"}}))
        self.assertEqual(self.client.get('/api/company').get_json()['company']['name'], "Updated company")

    def test_activity_metrics_count_public_delivery_records_before_filtering(self):
        self.coordinator.state['activity'] = [
            {'id': str(index), 'timestamp': '2026-10-30T09:00:00+00:00',
             'event': 'email', 'description': 'Email', 'type': 'email', 'direction': direction}
            for index, direction in enumerate(('sent', 'sent', 'received'))
        ]
        payload = self.client.get('/api/activity?type=message').get_json()
        self.assertEqual(payload['activity'], [])
        self.assertEqual(payload['metrics'], {'emails_sent': 2, 'emails_received': 1, 'messages': 0, 'open_experiments': 0})
        self.coordinator.fail_company_reads = True
        partial = self.client.get('/api/activity').get_json()
        self.assertIsNone(partial['metrics']['emails_sent'])
        self.assertIsNone(partial['metrics']['emails_received'])
        self.assertIsNone(partial['metrics']['open_experiments'])
        self.assertIn('error', partial)

    def csrf_token(self, task_id="admin-make-spreadsheet"):
        response = self.client.get('/api/session')
        self.assertEqual(response.status_code, 200)
        return response.get_json()['csrf_token']

    def start_run(self, task_id, idempotency_key):
        return self.client.post(
            f"/experiments/{task_id}/runs",
            data={"csrf_token": self.csrf_token(task_id), "idempotency_key": idempotency_key},
        )

    def run_rows(self, client=None):
        client = client or self.client
        self.authenticate(client)
        response = client.get("/api/runs")
        self.assertEqual(response.status_code, 200)
        return response.get_json()["runs"]

    def test_private_operator_routes_return_empty_401_without_dispatch_or_scores(self):
        anonymous = self.app.test_client()
        for path in (
            "/api/catalog", "/api/compatibility", "/api/company", "/api/activity",
            "/api/runs", "/api/events", "/api/runs/private-run",
            "/api/runs/private-run/evaluations", "/api/runs/private-run/evaluations/latest",
        ):
            with self.subTest(path=path):
                response = anonymous.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.data, b"")
        for path in ("/company/start", "/api/company/background/coffeebench/control", "/experiments/A1/runs", "/runs/private-run/evaluate"):
            with self.subTest(path=path):
                response = anonymous.post(path, data={})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.data, b"")
        self.assertEqual(self.rocket.dispatches, [])
        self.assertEqual(self.coordinator.start_keys, [])

    def test_operator_key_login_logout_csrf_and_signed_session_survive_app_restart(self):
        anonymous = self.app.test_client()
        csrf = anonymous.get('/api/session').get_json()['csrf_token']
        rejected = anonymous.post('/api/operator/login', data={
            'csrf_token': csrf, 'key': 'wrong-key',
        })
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(rejected.get_json(), {'error': 'operator_key_invalid'})
        unicode_rejected = anonymous.post('/api/operator/login', data={
            'csrf_token': csrf, 'key': '🔐clé-incorrecte',
        })
        self.assertEqual(unicode_rejected.status_code, 401)
        self.assertEqual(unicode_rejected.get_json(), {'error': 'operator_key_invalid'})
        self.assertFalse(anonymous.get('/api/operator/status').get_json()['authenticated'])
        no_csrf = anonymous.post('/api/operator/login', data={'key': self.operator_key_file.read_text().strip()})
        self.assertEqual(no_csrf.status_code, 400)

        accepted_csrf = anonymous.get('/api/session').get_json()['csrf_token']
        accepted = anonymous.post('/api/operator/login', data={
            'csrf_token': accepted_csrf, 'key': self.operator_key_file.read_text().strip(),
        })
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(anonymous.get('/api/operator/status').get_json()['authenticated'])
        cookie = anonymous.get_cookie('session')
        self.assertIsNotNone(cookie)
        self.assertTrue(cookie.http_only)

        restarted_client = self.make_app().test_client()
        restarted_client.set_cookie('session', cookie.value)
        self.assertTrue(restarted_client.get('/api/operator/status').get_json()['authenticated'])
        self.assertEqual(restarted_client.get('/api/catalog').status_code, 200)

        csrf = anonymous.get('/api/session').get_json()['csrf_token']
        logged_out = anonymous.post('/api/operator/logout', data={'csrf_token': csrf})
        self.assertEqual(logged_out.status_code, 200)
        self.assertFalse(anonymous.get('/api/operator/status').get_json()['authenticated'])
        self.assertEqual(anonymous.get('/api/catalog').status_code, 401)

    def test_public_shell_author_files_webhook_and_health_remain_available(self):
        anonymous = self.app.test_client()
        shell = Path(self.temp_dir.name) / "static"
        (shell / "app").mkdir(parents=True)
        (shell / "app/index.html").write_text("<main>public shell</main>")
        self.app.static_folder = str(shell)
        with anonymous.get('/') as response:
            self.assertEqual(response.status_code, 200)
        with anonymous.get('/runs') as response:
            self.assertEqual(response.status_code, 200)
        with anonymous.get('/runs/private-path') as response:
            self.assertIn(b"public shell", response.data)
        with anonymous.get('/static/app/index.html') as response:
            self.assertEqual(response.status_code, 200)
        self.assertEqual(anonymous.get('/api/health').get_json(), {'status': 'ok'})
        with anonymous.get('/experiments/hr-new-grad-job-description/inputs/0') as response:
            self.assertEqual(response.status_code, 200)

        with patch.dict(os.environ, {"EXPERIMENTS_WEBHOOK_TOKEN": "webhook-secret"}):
            webhook = anonymous.post('/api/chat-events', json={
                'token': 'webhook-secret', 'channel_id': 'public-webhook-fixture',
            })
        self.assertEqual(webhook.status_code, 200)

    def test_missing_operator_key_is_a_scoped_503_and_does_not_take_down_health(self):
        app = create_app(
            {
                "TESTING": True, "SECRET_KEY": "integration-test-secret",
                "DATABASE": str(Path(self.temp_dir.name) / "missing-key.sqlite"),
                "REPO": str(REPOSITORY_ROOT), "OPERATOR_KEY_FILE": str(Path(self.temp_dir.name) / "absent-key"),
                "SESSION_COOKIE_SECURE": False,
            },
            client_factory=lambda: self.rocket, coordinator_factory=lambda: self.coordinator,
        )
        self.apps.append(app)
        client = app.test_client()
        csrf = client.get('/api/session').get_json()['csrf_token']
        response = client.post('/api/operator/login', data={'csrf_token': csrf, 'key': 'candidate'})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {'error': 'operator_access_unavailable'})
        self.assertEqual(client.get('/api/health').status_code, 200)

    def test_operator_session_cookie_security_follows_configured_public_scheme(self):
        for scheme, secure in (("http", False), ("https", True)):
            with self.subTest(scheme=scheme):
                app = create_app(
                    {
                        "TESTING": True, "SECRET_KEY": "integration-test-secret",
                        "DATABASE": str(Path(self.temp_dir.name) / f"{scheme}-cookie.sqlite"),
                        "REPO": str(REPOSITORY_ROOT), "OPERATOR_KEY_FILE": str(self.operator_key_file),
                        "EXPERIMENTS_PUBLIC_URL": f"{scheme}://experiments.example.test",
                    },
                    client_factory=lambda: self.rocket, coordinator_factory=lambda: self.coordinator,
                )
                self.apps.append(app)
                client = app.test_client()
                base_url = f"{scheme}://experiments.example.test"
                session_response = client.get('/api/session', base_url=base_url)
                cookie = session_response.headers["Set-Cookie"]
                self.assertIn("HttpOnly", cookie)
                self.assertIn("SameSite=Lax", cookie)
                self.assertEqual("Secure" in cookie, secure)
                logged_in = client.post('/api/operator/login', base_url=base_url, data={
                    'csrf_token': session_response.get_json()['csrf_token'],
                    'key': self.operator_key_file.read_text().strip(),
                })
                self.assertEqual(logged_in.status_code, 200)
                self.assertTrue(client.get('/api/operator/status', base_url=base_url).get_json()['authenticated'])

    def test_operator_key_file_permissions_must_be_private(self):
        unprotected_key = Path(self.temp_dir.name) / "group-readable-operator-key"
        unprotected_key.write_text("test-key")
        unprotected_key.chmod(0o644)
        app = create_app(
            {
                "TESTING": True, "SECRET_KEY": "integration-test-secret",
                "DATABASE": str(Path(self.temp_dir.name) / "permission-check.sqlite"),
                "REPO": str(REPOSITORY_ROOT), "OPERATOR_KEY_FILE": str(unprotected_key),
                "SESSION_COOKIE_SECURE": False,
            },
            client_factory=lambda: self.rocket, coordinator_factory=lambda: self.coordinator,
        )
        self.apps.append(app)
        client = app.test_client()
        csrf = client.get('/api/session').get_json()['csrf_token']
        response = client.post('/api/operator/login', data={'csrf_token': csrf, 'key': 'test-key'})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {'error': 'operator_access_unavailable'})

    def wait_for_sse_status(self, run_id, status):
        response = self.client.get("/api/events", buffered=False)
        try:
            for chunk in response.response:
                if isinstance(chunk, bytes):
                    chunk = chunk.decode()
                data_line = next((line[6:] for line in chunk.splitlines() if line.startswith("data: ")), None)
                if data_line is None:
                    continue
                event_line = next((line[7:] for line in chunk.splitlines() if line.startswith("event: ")), None)
                if event_line != 'runs': continue
                snapshot = json.loads(data_line)
                if any(run["id"] == run_id and run["status"] == status for run in snapshot["runs"]):
                    return
            self.fail(f"event stream ended before run {run_id} reached {status}")
        finally:
            response.close()

    def test_post_requires_session_csrf_token_and_accepts_valid_form(self):
        response = self.client.post(
            "/experiments/admin-make-spreadsheet/runs",
            data={"idempotency_key": "csrf-protected"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.rocket.dispatches, [])

        response = self.start_run("admin-make-spreadsheet", "csrf-protected")
        self.assertEqual(response.status_code, 303)
        self.rocket.wait_for_dispatches(1)
        self.rocket.wait_for_thread_completions(1)
        self.assertEqual(len(self.rocket.dispatches), 1)

    def test_duplicate_run_submission_with_same_key_redirects_to_same_durable_run(self):
        self.rocket.dispatch_gate.clear()
        first = self.start_run("admin-make-spreadsheet", "same-browser-submit")
        self.assertEqual(first.status_code, 303)
        first_location = first.headers["Location"]
        self.rocket.wait_for_dispatches(1)

        duplicate = self.start_run("admin-make-spreadsheet", "same-browser-submit")
        self.assertEqual(duplicate.status_code, 303)
        self.assertEqual(duplicate.headers["Location"], first_location)
        self.assertEqual(len(self.run_rows()), 1)
        self.assertEqual(len(self.rocket.dispatches), 1)

        self.rocket.dispatch_gate.set()
        self.rocket.wait_for_thread_completions(1)

    def test_two_runs_remain_independently_tracked_after_app_restart(self):
        first = self.start_run("admin-make-spreadsheet", "independent-run-one")
        second = self.start_run("hr-new-grad-job-description", "independent-run-two")
        self.assertEqual(first.status_code, 303)
        self.assertEqual(second.status_code, 303)
        first_id = first.headers["Location"].rsplit("/", 1)[-1]
        second_id = second.headers["Location"].rsplit("/", 1)[-1]
        self.assertNotEqual(first_id, second_id)
        self.rocket.wait_for_dispatches(2)
        self.rocket.wait_for_thread_completions(2)

        restarted_client = self.make_app().test_client()
        persisted = {run["id"]: run for run in self.run_rows(restarted_client)}
        self.assertEqual(set(persisted), {first_id, second_id})
        self.assertEqual(persisted[first_id]["experiment_id"], "admin-make-spreadsheet")
        self.assertEqual(persisted[second_id]["experiment_id"], "hr-new-grad-job-description")
        self.assertEqual(persisted[first_id]["status"], "awaiting_viv")
        self.assertEqual(persisted[second_id]["status"], "awaiting_viv")

    def test_webhook_authenticates_and_tracks_viv_reply(self):
        self.rocket.thread_gate.clear()
        response = self.start_run("admin-make-spreadsheet", "webhook-run")
        run_id = response.headers["Location"].rsplit("/", 1)[-1]
        self.rocket.wait_for_dispatches(1)
        self.rocket.wait_for_threads(1)
        self.rocket.thread_gate.set()
        self.rocket.wait_for_thread_completions(1)

        with self.subTest("invalid token is rejected"):
            with patch.dict(os.environ, {"EXPERIMENTS_WEBHOOK_TOKEN": "webhook-secret"}):
                rejected = self.client.post(
                    "/api/chat-events", json={"token": "wrong", "channel_id": f"room-{run_id}"}
                )
            self.assertEqual(rejected.status_code, 403)
        self.assertEqual(len(self.rocket.thread_calls), 1)

        with self.subTest("authenticated reply changes the durable run state"):
            self.rocket.messages_by_room[f"room-{run_id}"] = [
                {"author": "viv", "text": "I’ve prepared the requested spreadsheet.", "timestamp": "2026-10-29T10:00:00+00:00"}
            ]
            with patch.dict(os.environ, {"EXPERIMENTS_WEBHOOK_TOKEN": "webhook-secret"}):
                accepted = self.client.post(
                    "/api/chat-events",
                    json={"token": "webhook-secret", "channel_id": f"room-{run_id}"},
                )
            self.assertEqual(accepted.status_code, 200)
            self.rocket.wait_for_threads(2)
            self.rocket.wait_for_thread_completions(2)
            self.wait_for_sse_status(run_id, "viv_replied")

        current = next(run for run in self.run_rows() if run["id"] == run_id)
        self.assertEqual(current["status"], "viv_replied")
        self.assertIn("requested spreadsheet", current["messages"][0]["text"])

    def test_react_api_preserves_brief_and_returns_new_run_location(self):
        library=self.client.get('/api/catalog').get_json()['experiments']
        brief=next(item for item in library if item['id']=='hr-new-grad-job-description')
        expected=(REPOSITORY_ROOT/'workspaces/tasks/hr-new-grad-job-description/task.md').read_text()
        self.assertEqual(brief['description'],expected)
        session=self.client.get('/api/session').get_json()
        response=self.client.post('/experiments/hr-new-grad-job-description/runs',
            data=session,headers={'Accept':'application/json'})
        self.assertEqual(response.status_code,200)
        created=response.get_json()
        self.rocket.wait_for_thread_completions(1)
        run=self.client.get('/api/runs/'+created['run_id']).get_json()['run']
        self.assertEqual(created['url'],'/runs/'+run['id'])
        self.assertEqual(run['description'],expected)
        self.assertIsInstance(run['messages'],list)
        self.assertNotIn('idempotency_key',run)
        self.assertEqual(len(self.rocket.dispatches[0][3]),2)

    def test_business_display_copy_has_no_urls_and_dispatch_preserves_original(self):
        library=self.client.get('/api/catalog').get_json()['experiments']
        workplace=[brief for brief in library if brief['kind']=='workplace']
        simulations=[brief for brief in library if brief['kind']=='simulation' and brief.get('scenario_id')!='TAU_RETAIL']
        retail=[brief for brief in library if brief.get('scenario_id')=='TAU_RETAIL']
        self.assertEqual(len(retail),114)
        self.assertEqual({item['role'] for item in retail},{'Customer support'})
        self.assertEqual(len(workplace),105)
        self.assertEqual({item['id'] for item in simulations},{'A1','B1','C1','D1','E1','S01'})
        by_id={item['id']:item for item in simulations}
        self.assertEqual({item['role'] for item in simulations},{'Finance'})
        self.assertEqual(by_id['A1']['dependencies'],['ERPNext'])
        self.assertEqual(by_id['B1']['dependencies'],['SimPy','Email'])
        self.assertEqual(by_id['C1']['benchmarks'],['CoffeeBench'])
        self.assertEqual(by_id['D1']['benchmarks'],['E-Commerce Bench'])
        self.assertEqual(by_id['E1']['benchmarks'],['τ-bench'])
        self.assertIn('required authorization',by_id['S01']['display_description'])
        self.assertNotIn('hidden from the tested agent',by_id['S01']['display_description'])
        for brief in workplace:
            with self.subTest(task=brief['id']):
                self.assertTrue(brief['summary'].strip())
                self.assertTrue(brief['display_description'].strip())
                self.assertNotRegex(brief['summary']+' '+brief['display_description'],r'https?://|www\.')
        task='admin-read-survey-and-summarise'
        shown=next(brief for brief in library if brief['id']==task)
        self.assertIn('three most popular beverage choices',shown['display_description'].lower())
        self.assertIn('Chen Xinyi',shown['display_description'])
        response=self.start_run(task,'display-only-change')
        self.assertEqual(response.status_code,303)
        self.rocket.wait_for_thread_completions(1)
        original=(REPOSITORY_ROOT/'workspaces/tasks'/task/'task.md').read_text()
        self.assertEqual(self.rocket.dispatches[0][2],original)
        self.assertIn('http://the-agent-company.com:8092/',original)

    def test_company_start_pause_and_status_use_coordinator_boundary_with_csrf(self):
        self.coordinator.state.update(status='running',simulated_time='2026-10-30T09:00:00+00:00',erp_site_id='company-site')
        csrf=self.csrf_token()
        rejected=self.client.post('/company/start',data={})
        self.assertEqual(rejected.status_code,400)
        started=self.client.post('/company/start',data={'csrf_token':csrf,'idempotency_key':'company-start-1'},headers={'Accept':'application/json'})
        self.assertEqual(started.status_code,200)
        self.assertEqual(started.get_json()['company']['status'],'running')
        self.assertEqual(self.coordinator.start_keys,[])
        paused=self.client.post('/company/pause',data={
            'csrf_token':csrf,'paused':'true','idempotency_key':'pause-company-1'},
            headers={'Accept':'application/json'})
        self.assertEqual(paused.status_code,200)
        self.assertEqual(paused.get_json()['company']['status'],'paused')
        self.assertEqual(self.coordinator.pause_requests,[(True,'pause-company-1')])
        public=self.client.get('/api/company').get_json()['company']
        self.assertEqual(public['status'],'paused')
        self.assertNotIn('private_record',public)
        resumed=self.client.post('/company/start',data={'csrf_token':csrf,'idempotency_key':'company-resume-1'})
        self.assertEqual(resumed.status_code,200)
        self.assertEqual(resumed.get_json()['company']['status'],'running')
        self.assertEqual(self.coordinator.pause_requests[-1],(False,'company-resume-1'))

    def test_resume_uses_request_idempotency_key_and_preserves_failure(self):
        self.coordinator.state.update(status='paused',paused=True)
        self.coordinator.fail_pause_calls=1
        first=self.client.post('/company/start',data={'csrf_token':self.csrf_token(),
            'idempotency_key':'resume-key'})
        self.assertEqual(first.status_code,502)
        retry=self.client.post('/company/start',data={'csrf_token':self.csrf_token(),
            'idempotency_key':'resume-key'})
        self.assertEqual(retry.status_code,200)
        self.assertEqual(self.coordinator.pause_requests,[(False,'resume-key'),(False,'resume-key')])
        self.assertIsNone(retry.get_json()['company']['error'])

    def test_coffeebench_control_requires_csrf_and_forwards_provider_action(self):
        denied=self.client.post('/api/company/background/coffeebench/control',
            data={'enabled':'false'},headers={'Idempotency-Key':'coffee-stop'})
        self.assertEqual(denied.status_code,400)
        response=self.client.post('/api/company/background/coffeebench/control',
            data={'csrf_token':self.csrf_token(),'enabled':'false'},
            headers={'Idempotency-Key':'coffee-stop'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.get_json(),{
            'provider_id':'coffeebench','enabled':False,'in_flight':False,'status':'stopped'})
        self.assertEqual(self.coordinator.background_control_requests,[('coffeebench',False,'coffee-stop')])

    def test_ecommercebench_control_requires_csrf_and_forwards_provider_action(self):
        denied=self.client.post('/api/company/background/ecommercebench/control',
            data={'enabled':'false'},headers={'Idempotency-Key':'commerce-stop'})
        self.assertEqual(denied.status_code,400)
        response=self.client.post('/api/company/background/ecommercebench/control',
            data={'csrf_token':self.csrf_token(),'enabled':'false'},
            headers={'Idempotency-Key':'commerce-stop'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.get_json(),{
            'provider_id':'ecommercebench','enabled':False,'in_flight':False,'status':'stopped'})
        self.assertEqual(self.coordinator.background_control_requests,[('ecommercebench',False,'commerce-stop')])
        self.assertEqual(self.client.post('/api/company/background/unknown/control',
            data={'csrf_token':self.csrf_token(),'enabled':'false'},
            headers={'Idempotency-Key':'unknown-stop'}).status_code,404)

    def test_coffeebench_idempotent_start_replay_returns_current_stopped_state(self):
        self.coordinator.control_background_provider=lambda provider_id, enabled, *, idempotency_key: {
            'provider_id':'coffeebench','enabled':False,'in_flight':False,'status':'stopped'}
        response=self.client.post('/api/company/background/coffeebench/control',
            data={'csrf_token':self.csrf_token(),'enabled':'true'},
            headers={'Idempotency-Key':'old-coffee-start'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.get_json(),{
            'provider_id':'coffeebench','enabled':False,'in_flight':False,'status':'stopped'})

    def test_malformed_company_status_is_not_returned_as_success(self):
        self.coordinator.fail_company_reads=True
        response=self.client.get('/api/company')
        self.assertEqual(response.status_code,503)
        self.assertIn('could not be retrieved',response.get_json()['error'])

    def test_successful_status_read_does_not_erase_a_prior_operational_error(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE company_state SET error='Previous start outcome needs reconciliation' WHERE singleton=1")
        response=self.client.get('/api/company')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.get_json()['company']['error'],'Previous start outcome needs reconciliation')

    def test_company_status_keeps_provider_uncertainty_visible_and_redacts_internal_background_data(self):
        self.coordinator.state.update(status='running',paused=False,simulated_time='2026-10-30T12:00:00+00:00')
        self.coordinator.state['background']={
            'initialized':False,
            'providers':{'coffeebench':{'status':'error','initialized':True,
                                         'next_due':'2026-10-30T09:00:00+00:00',
                                         'private_snapshot':'must not leave server'}},
            'errors':{
                'coffeebench':{'code':'background_operation_uncertain',
                               'pending_operation_id':'private-operation-id',
                               'private_model_detail':'must not leave server'},
                'company_background':{'code':'RuntimeError','private_detail':'must not leave server'},
                'company_feedback_reconcile':{'code':'feedback_reconciliation_failed',
                                              'private_event_id':'private-feedback-event'},
                'native_finance_reconcile':{'code':'reconciliation_failed','document_name':'private-document'},
            },
            'obligations':[{'account':'private-account','amount':123.45}],
        }

        response=self.client.get('/api/company')

        self.assertEqual(response.status_code,200)
        company=response.get_json()['company']
        self.assertEqual(company['status'],'running')
        self.assertEqual(company['background']['errors']['coffeebench'],
                         {'code':'background_operation_uncertain'})
        self.assertEqual(company['background']['errors']['company_background'],{'code':'RuntimeError'})
        self.assertEqual(company['background']['errors']['company_feedback_reconcile'],
                         {'code':'feedback_reconciliation_failed'})
        self.assertEqual(company['background']['errors']['native_finance_reconcile'],
                         {'code':'reconciliation_failed'})
        self.assertEqual(company['background']['providers']['coffeebench']['status'],'error')
        self.assertNotIn('obligations',company['background'])
        self.assertNotIn('private_snapshot',company['background']['providers']['coffeebench'])
        self.assertNotIn('private-operation-id',response.get_data(as_text=True))
        self.assertNotIn('private_model_detail',response.get_data(as_text=True))
        self.assertNotIn('private-document',response.get_data(as_text=True))
        self.assertNotIn('private_detail',response.get_data(as_text=True))
        self.assertNotIn('private-feedback-event',response.get_data(as_text=True))

    def test_simulation_rejection_is_failed_with_actionable_company_state(self):
        response=self.start_run('A1','company-not-started')
        run_id=response.headers['Location'].rsplit('/',1)[-1]
        self.coordinator.wait_for_experiments(1)
        self.wait_for_sse_status(run_id,'failed')
        run=self.client.get('/api/runs/'+run_id).get_json()['run']
        self.assertIn('Company state rejected this experiment',run['error'])
        self.assertEqual(run['status'],'failed')

    def test_uncertain_injection_retry_reuses_remote_experiment_identity(self):
        self.coordinator.state.update(status='running',simulated_time='2026-10-30T09:00:00+00:00',erp_site_id='company-site')
        self.coordinator.experiment_failure=CoordinatorError('company/experiments','timeout')
        response=self.start_run('C1','accepted-but-timeout')
        run_id=response.headers['Location'].rsplit('/',1)[-1]
        self.coordinator.wait_for_experiments(1)
        self.wait_for_sse_status(run_id,'uncertain')
        first_external_id=self.coordinator.state['experiments'][0]['experiment_id']
        retry=self.client.post(f'/runs/{run_id}/retry',data={'csrf_token':self.csrf_token()})
        self.assertEqual(retry.status_code,303)
        self.coordinator.wait_for_experiments(2)
        self.wait_for_sse_status(run_id,'running')
        tracked=self.client.get('/api/runs/'+run_id).get_json()['run']
        self.assertEqual(tracked['simulation_experiment_id'],first_external_id)
        self.assertEqual(len(self.coordinator.state['experiments']),1)
        self.assertEqual([key for _,key in self.coordinator.experiment_requests],[run_id,run_id])

    def test_sse_polls_company_read_only_and_emits_activity_refresh(self):
        self.app.config['EVENTS_WAIT_SECONDS']=0.01
        response=self.client.get('/api/events',buffered=False)
        iterator=iter(response.response)
        try:
            next(iterator);next(iterator);next(iterator)  # initial runs, company and activity events
            event=next(iterator)  # bounded heartbeat performs coordinator GET /company
            self.coordinator.wait_for_company_reads(1)
            self.assertIn(b'event: company',event)
        finally:
            response.close()

    def test_simulation_launch_durably_links_to_shared_company_experiment(self):
        self.coordinator.state.update(status='running',simulated_time='2026-10-30T09:00:00+00:00',erp_site_id='company-site')
        first=self.start_run('A1','simulation-launch-key')
        self.assertEqual(first.status_code,303)
        run_id=first.headers['Location'].rsplit('/',1)[-1]
        self.coordinator.wait_for_experiments(1)
        duplicate=self.start_run('A1','simulation-launch-key')
        self.assertEqual(duplicate.headers['Location'],first.headers['Location'])
        self.assertEqual(self.coordinator.experiment_requests,[('A1',run_id)])
        self.wait_for_sse_status(run_id,'running')
        tracked=self.client.get('/api/runs/'+run_id).get_json()['run']
        self.assertEqual(tracked['kind'],'simulation')
        self.assertEqual(tracked['company_id'],'company')
        self.assertEqual(tracked['simulation_experiment_id'],self.coordinator.state['experiments'][0]['experiment_id'])
        self.assertEqual(tracked['activity'][0]['description'],'A new company case was opened.')
        self.assertNotIn('private_record',tracked['activity'][0])

        conflict=self.start_run('B1','simulation-launch-key')
        self.assertEqual(conflict.status_code,409)

    def test_evaluation_proxy_persists_history_scopes_reports_and_filters_private_fields(self):
        self.coordinator.state.update(status='running',simulated_time='2026-10-30T09:00:00+00:00')
        launched=self.start_run('C1','evaluation-proxy-run')
        local_run_id=launched.headers['Location'].rsplit('/',1)[-1]
        self.coordinator.wait_for_experiments(1)
        self.wait_for_sse_status(local_run_id,'running')
        external_run_id=self.coordinator.state['experiments'][0]['experiment_id']

        initial=self.client.get(f'/api/runs/{local_run_id}/evaluations').get_json()
        latest=self.client.get(f'/api/runs/{local_run_id}/evaluations/latest').get_json()
        self.assertEqual(initial,{'evaluations':[],'not_evaluated':True})
        self.assertEqual(latest,{'evaluation':None,'not_evaluated':True})

        response=self.client.post(f'/runs/{local_run_id}/evaluate',data={
            'csrf_token':self.csrf_token(),'idempotency_key':'evaluation-key-one',
        })
        self.assertEqual(response.status_code,200)
        result=response.get_json()['evaluation']
        self.assertEqual(self.coordinator.evaluation_requests,[(external_run_id,'evaluation-key-one')])
        self.assertEqual(result['run_id'],external_run_id)
        self.assertEqual(result['scores'],{'reward':0.5,'complete':False})
        self.assertEqual(result['native_scale'],{'reward':'native_reward_0_to_1','complete':'boolean'})
        self.assertNotIn('prompt',result)
        self.assertNotIn('evidence_snapshot',result)
        self.assertNotIn('private_evidence',result['criterion_results'][0])

        history=self.client.get(f'/api/runs/{local_run_id}/evaluations').get_json()['evaluations']
        self.assertEqual([item['evaluation_id'] for item in history],[result['evaluation_id']])
        latest=self.client.get(f'/api/runs/{local_run_id}/evaluations/latest').get_json()['evaluation']
        self.assertEqual(latest['evaluation_id'],result['evaluation_id'])
        detail=self.client.get(
            f'/api/runs/{local_run_id}/evaluations/{result["evaluation_id"]}'
        )
        self.assertEqual(detail.status_code,200)
        self.assertEqual(detail.get_json()['evaluation']['evidence_ref'],result['evidence_ref'])

        for index,status in enumerate(('incomplete','failed'),start=2):
            self.coordinator.evaluation_status=status
            scored=self.client.post(f'/runs/{local_run_id}/evaluate',data={
                'csrf_token':self.csrf_token(),'idempotency_key':f'evaluation-key-{index}',
            }).get_json()['evaluation']
            self.assertEqual(scored['status'],status)
            if status=='incomplete':
                self.assertEqual(scored['scores'],{'reward':None})
            else:
                self.assertEqual(scored['scores'],{})
                self.assertEqual(scored['failure']['code'],'benchmark_unavailable')
                self.assertIsNone(scored['evaluation_mode'])

        other=self.start_run('B1','evaluation-proxy-other-run')
        other_id=other.headers['Location'].rsplit('/',1)[-1]
        self.coordinator.wait_for_experiments(2)
        self.wait_for_sse_status(other_id,'running')
        cross_run=self.client.get(
            f'/api/runs/{other_id}/evaluations/{result["evaluation_id"]}'
        )
        self.assertEqual(cross_run.status_code,404)

    def test_workplace_run_has_explicit_not_evaluated_response_without_remote_evaluator_call(self):
        started=self.start_run('admin-make-spreadsheet','workplace-no-score')
        local_run_id=started.headers['Location'].rsplit('/',1)[-1]
        response=self.client.get(f'/api/runs/{local_run_id}/evaluations')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.get_json(),{'evaluations':[],'not_evaluated':True})
        self.assertEqual(self.coordinator.evaluation_requests,[])

    def test_activity_feed_combines_public_chat_and_coordinator_activity(self):
        self.coordinator.state.update(status='running',simulated_time='2026-10-30T09:00:00+00:00',erp_site_id='company-site')
        workplace=self.start_run('admin-make-spreadsheet','activity-workplace-run')
        workplace_id=workplace.headers['Location'].rsplit('/',1)[-1]
        self.rocket.wait_for_thread_completions(1)
        self.rocket.messages_by_room[f'room-{workplace_id}']=[{'author':'viv','text':'Spreadsheet saved.',
            'timestamp':'2026-10-30T09:00:30+00:00'}]
        self.client.post(f'/runs/{workplace_id}/refresh',data={'csrf_token':self.csrf_token()})
        launched=self.start_run('E1','activity-run')
        self.coordinator.wait_for_experiments(1)
        simulation_run_id=launched.headers['Location'].rsplit('/',1)[-1]
        self.wait_for_sse_status(simulation_run_id,'running')
        self.coordinator.state['activity'].append({'id':'public-email','timestamp':'2026-10-30T09:01:00+00:00',
            'event':'customer_email','description':'A customer message was delivered.',
            'type':'email','title':'Customer email','content':'Please review the order.',
            'experiment_id':self.coordinator.state['experiments'][0]['experiment_id'],
            'private_record':{'expected_answer':'secret'}})
        # Refresh obtains only the coordinator's sanitized controller view.
        feed=self.client.get('/api/activity').get_json()['activity']
        email=next(item for item in feed if item['id']=='public-email')
        self.assertEqual(email['type'],'email')
        self.assertEqual(email['run_id'],simulation_run_id)
        self.assertNotIn('private_record',email)
        chat=next(item for item in feed if item['run_id']==workplace_id)
        self.assertEqual(chat['type'],'message')
        self.assertEqual(chat['content'],'Spreadsheet saved.')
        self.assertEqual(chat['direction'],'received')
        filtered=self.client.get('/api/activity?type=email').get_json()['activity']
        self.assertEqual([item['type'] for item in filtered],['email'])

    def test_input_download_only_exposes_manifest_selected_source(self):
        response=self.client.get('/experiments/hr-new-grad-job-description/inputs/0')
        self.assertEqual(response.status_code,200)
        expected=(REPOSITORY_ROOT/'workspaces/tasks/hr-new-grad-job-description/requirement.txt').read_bytes()
        self.assertEqual(response.data,expected)
        response.close()
        self.assertEqual(self.client.get('/experiments/hr-new-grad-job-description/inputs/2').status_code,404)
        self.assertEqual(self.client.get('/experiments/hr-new-grad-job-description/inputs/evaluate.py').status_code,404)

    def test_thread_failure_keeps_confirmed_delivery_and_can_recover(self):
        original=self.rocket.thread
        def unavailable(room_id,message_id):
            raise ConnectionError('chat temporarily unavailable')
        self.rocket.thread=unavailable
        with self.assertLogs(level='ERROR') as captured:
            response=self.start_run('admin-make-spreadsheet','delivery-with-refresh-failure')
            run_id=response.headers['Location'].rsplit('/',1)[-1]
            stream=self.client.get('/api/events',buffered=False)
            try:
                for chunk in stream.response:
                    current=next(run for run in self.run_rows() if run['id']==run_id)
                    if current['error']: break
            finally:
                stream.close()
        self.assertEqual(current['status'],'awaiting_viv')
        self.assertEqual(current['message_id'],'exp-'+run_id)
        self.assertIn('Request delivered',current['error'])
        self.assertTrue(any('Initial thread refresh failed' in line for line in captured.output))
        self.rocket.thread=original
        refreshed=self.client.post('/runs/'+run_id+'/refresh',data={'csrf_token':self.csrf_token()},headers={'Accept':'application/json'})
        self.assertEqual(refreshed.status_code,200)
        current=self.client.get('/api/runs/'+run_id).get_json()['run']
        self.assertIsNone(current['error'])
        self.assertEqual(len(self.rocket.dispatches),1)

    def test_restart_recovers_persisted_sending_run_as_uncertain(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """INSERT INTO runs(id,experiment_id,title,description,status,created_at,idempotency_key)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    "run-left-sending",
                    "admin-make-spreadsheet",
                    "Make spreadsheet",
                    "Prepare the spreadsheet.",
                    "sending",
                    "2026-10-29T10:00:00+00:00",
                    "crashed-process-run",
                ),
            )

        restarted_client = self.make_app().test_client()
        persisted = next(run for run in self.run_rows(restarted_client) if run["id"] == "run-left-sending")
        self.assertEqual(persisted["status"], "uncertain")
        self.assertIn("server restarted", persisted["error"])
        self.assertEqual(self.rocket.dispatches, [])


if __name__ == "__main__":
    unittest.main()
