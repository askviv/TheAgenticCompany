"""Tests for the authenticated coordinator HTTP boundary without live requests."""

from __future__ import annotations

import unittest
from unittest.mock import Mock

import requests

from servers.experiments.coordinator import CoordinatorError, SimulationCoordinatorClient


class CoordinatorClientTests(unittest.TestCase):
    def test_background_provider_control_uses_controller_auth_and_idempotency(self):
        response=Mock()
        response.json.return_value={'provider_id':'coffeebench','enabled':False,'in_flight':True,'status':'stopping'}
        session=Mock()
        session.request.return_value=response
        client=SimulationCoordinatorClient('http://coordinator:8131','private-token',session=session)

        result=client.control_background_provider('ecommercebench',False,idempotency_key='commerce-stop-uuid')

        self.assertEqual(result['status'],'stopping')
        session.request.assert_called_once_with(
            'POST','http://coordinator:8131/company/background/ecommercebench/control',
            headers={'Authorization':'Bearer private-token','Idempotency-Key':'commerce-stop-uuid'},
            timeout=15.0,json={'enabled':False})

    def test_company_experiment_posts_server_side_auth_and_durable_idempotency_key(self):
        response=Mock()
        response.json.return_value={'experiment_id':'case-uuid','status':'running'}
        session=Mock()
        session.request.return_value=response
        client=SimulationCoordinatorClient('http://coordinator:8131','private-token',session=session)

        result=client.start_experiment('A1',idempotency_key='dashboard-run-uuid')

        self.assertEqual(result,{'experiment_id':'case-uuid','status':'running'})
        session.request.assert_called_once_with(
            'POST','http://coordinator:8131/company/experiments',
            headers={'Authorization':'Bearer private-token','Idempotency-Key':'dashboard-run-uuid'},
            timeout=15.0,json={'scenario_id':'A1'})
        self.assertNotIn('private-token',repr(result))

    def test_durable_evaluation_routes_keep_auth_and_idempotency_at_proxy_boundary(self):
        response=Mock()
        response.json.return_value={'evaluation':{'evaluation_id':'eval-1'}}
        session=Mock()
        session.request.return_value=response
        client=SimulationCoordinatorClient('http://coordinator:8131','private-token',session=session)

        created=client.start_evaluation('run-1',idempotency_key='eval-key')
        legacy=client.evaluate('run-1',idempotency_key='legacy-eval-key')
        listed=client.evaluations('run-1')
        latest=client.latest_evaluation('run-1')
        detail=client.evaluation('eval-1')

        self.assertEqual(created,{'evaluation':{'evaluation_id':'eval-1'}})
        self.assertEqual(legacy,created)
        self.assertEqual(listed,created)
        self.assertEqual(latest,created)
        self.assertEqual(detail,created)
        self.assertEqual(session.request.call_args_list[0].args,('POST','http://coordinator:8131/runs/run-1/evaluations'))
        self.assertEqual(session.request.call_args_list[0].kwargs['headers'],{
            'Authorization':'Bearer private-token','Idempotency-Key':'eval-key'})
        self.assertEqual(session.request.call_args_list[0].kwargs['json'],{})
        self.assertEqual(session.request.call_args_list[1].args,('POST','http://coordinator:8131/scenarios/run-1/evaluate'))
        self.assertEqual(session.request.call_args_list[1].kwargs['headers'],{
            'Authorization':'Bearer private-token','Idempotency-Key':'legacy-eval-key'})
        self.assertEqual([call.args[0] for call in session.request.call_args_list[2:]],['GET']*3)

    def test_provider_error_body_is_not_exposed_in_safe_exception(self):
        response=Mock()
        response.status_code=503
        response.raise_for_status.side_effect=requests.HTTPError(response=response)
        session=Mock()
        session.request.return_value=response
        client=SimulationCoordinatorClient('http://coordinator:8131','private-token',session=session)

        with self.assertRaises(CoordinatorError) as raised:
            client.company()

        self.assertEqual(str(raised.exception),'Simulation coordinator /company failed (http_503).')
        self.assertNotIn('private-token',str(raised.exception))

    def test_server_error_preserves_only_a_uuid_correlation_id(self):
        response=Mock()
        response.status_code=503
        response.headers={'X-Correlation-ID':'30b95e99-1460-4b99-bb45-726bbfab517a'}
        response.raise_for_status.side_effect=requests.HTTPError(response=response)
        session=Mock()
        session.request.return_value=response
        client=SimulationCoordinatorClient('http://coordinator:8131','private-token',session=session)

        with self.assertRaises(CoordinatorError) as raised:
            client.company()

        self.assertEqual(raised.exception.correlation_id,'30b95e99-1460-4b99-bb45-726bbfab517a')
        self.assertIn('correlation_id=30b95e99-1460-4b99-bb45-726bbfab517a',str(raised.exception))

    def test_server_error_discards_non_uuid_correlation_values(self):
        response=Mock()
        response.status_code=503
        response.headers={'X-Correlation-ID':'raw-provider-secret'}
        response.raise_for_status.side_effect=requests.HTTPError(response=response)
        session=Mock()
        session.request.return_value=response
        client=SimulationCoordinatorClient('http://coordinator:8131','private-token',session=session)

        with self.assertRaises(CoordinatorError) as raised:
            client.company()

        self.assertIsNone(raised.exception.correlation_id)
        self.assertNotIn('raw-provider-secret',str(raised.exception))


if __name__=='__main__':
    unittest.main()
