"""
Tests for the Cloud Tasks handoff: enqueueing from the webhook, the task route and its OIDC check,
and the per-attempt checks that keep retries from leaving a second runner behind.
"""
import json
from unittest.mock import MagicMock, Mock, patch

import pytest
from google.api_core import exceptions as gapi_exceptions

from app.clients.gcloud_client import InstanceCreationError, QuotaExceededError, ZoneCapacityError
from app.clients.tasks_client import TasksClient
from app.services.webhook_service import WebhookService
from app.utils.security import verify_google_oidc_token

QUEUE = 'projects/test-project/locations/us-central1/queues/github-runners-provision'
MANAGER_URL = 'https://manager-123.us-central1.run.app'
INVOKER = 'github-runners-provisioner@test-project.iam.gserviceaccount.com'

QUEUED_PAYLOAD = {
    'action': 'queued',
    'workflow_job': {'id': 4242, 'labels': ['gcp-ubuntu-24-04-8core']},
    'repository': {
        'html_url': 'https://github.com/acme/app',
        'full_name': 'acme/app',
        'owner': {'html_url': 'https://github.com/acme'},
    },
    'organization': {'login': 'acme'},
}

TASK_BODY = {
    'job_id': 4242,
    'template_name': 'gcp-ubuntu-24-04-8core',
    'repo_url': 'https://github.com/acme/app',
    'repo_owner_url': 'https://github.com/acme',
    'repo_name': 'acme/app',
    'org_name': 'acme',
    'delivery_id': 'd-1',
}


@pytest.fixture
def queue_env(monkeypatch):
    monkeypatch.setenv('PROVISION_QUEUE', QUEUE)
    monkeypatch.setenv('MANAGER_URL', MANAGER_URL)
    monkeypatch.setenv('PROVISION_INVOKER_EMAIL', INVOKER)


class TestTasksClient:
    def test_disabled_without_full_configuration(self, monkeypatch):
        monkeypatch.setenv('PROVISION_QUEUE', QUEUE)
        monkeypatch.delenv('MANAGER_URL', raising=False)
        assert TasksClient().enabled is False

    def test_task_is_named_by_job_and_carries_an_oidc_token(self, queue_env):
        client = TasksClient()
        client._client = MagicMock()
        client._client.create_task.return_value.name = f'{QUEUE}/tasks/job-4242'

        assert client.enqueue_provision(TASK_BODY, 4242, delivery_id='d-1') == 'enqueued'

        request = client._client.create_task.call_args.kwargs['request']
        assert request.parent == QUEUE
        assert request.task.name == f'{QUEUE}/tasks/job-4242'
        assert request.task.http_request.url == f'{MANAGER_URL}/tasks/provision'
        assert request.task.http_request.oidc_token.service_account_email == INVOKER
        assert request.task.http_request.oidc_token.audience == MANAGER_URL
        assert json.loads(request.task.http_request.body) == TASK_BODY
        assert request.task.dispatch_deadline.seconds == 1800

    def test_redelivery_is_a_duplicate_not_a_second_task(self, queue_env):
        client = TasksClient()
        client._client = MagicMock()
        client._client.create_task.side_effect = gapi_exceptions.AlreadyExists('task exists')
        assert client.enqueue_provision(TASK_BODY, 4242) == 'duplicate'


@patch('app.services.webhook_service.GCloudClient')
@patch('app.services.webhook_service.GitHubClient')
class TestWebhookHandoff:
    def test_queued_job_is_enqueued_without_provisioning(self, gh, gc, queue_env):
        with patch.object(TasksClient, 'enqueue_provision', return_value='enqueued') as enqueue:
            result = WebhookService().handle_workflow_job(QUEUED_PAYLOAD, delivery_id='d-1')
        assert result == {'action': 'enqueued', 'runner_name': None}
        enqueue.assert_called_once_with(TASK_BODY, 4242, delivery_id='d-1')
        gc.return_value.create_runner_instance.assert_not_called()

    def test_enqueue_failure_falls_back_to_direct_provisioning(self, gh, gc, queue_env, monkeypatch):
        monkeypatch.delenv('ASYNC_PROVISIONING', raising=False)
        gh.return_value.get_registration_token.return_value = 'tok'
        gc.return_value.create_runner_instance.return_value = 'gcp-runner-x'
        with patch.object(TasksClient, 'enqueue_provision', side_effect=RuntimeError('queue down')):
            result = WebhookService().handle_workflow_job(QUEUED_PAYLOAD, delivery_id='d-1')
        assert result == {'action': 'created', 'runner_name': 'gcp-runner-x'}


@patch('app.services.webhook_service.GCloudClient')
@patch('app.services.webhook_service.GitHubClient')
class TestProvisionFromTask:
    def test_creates_the_vm_labelled_with_the_job(self, gh, gc):
        gc.return_value.find_live_instance_for_job.return_value = None
        gh.return_value.get_workflow_job_status.return_value = 'queued'
        gh.return_value.get_registration_token.return_value = 'tok'
        gc.return_value.create_runner_instance.return_value = 'gcp-runner-x'

        result = WebhookService().provision_from_task(dict(TASK_BODY))

        assert result['action'] == 'created'
        gh.return_value.get_registration_token.assert_called_once_with(org_name='acme', delivery_id='d-1')
        assert gc.return_value.create_runner_instance.call_args.kwargs['job_id'] == 4242

    def test_skips_a_job_that_already_has_a_vm(self, gh, gc):
        gc.return_value.find_live_instance_for_job.return_value = 'gcp-runner-old'
        result = WebhookService().provision_from_task(dict(TASK_BODY))
        assert result == {'action': 'skipped', 'runner_name': 'gcp-runner-old', 'reason': 'vm exists'}
        gc.return_value.create_runner_instance.assert_not_called()

    @pytest.mark.parametrize('status', ['in_progress', 'completed', None])
    def test_skips_a_job_that_is_no_longer_queued(self, gh, gc, status):
        gc.return_value.find_live_instance_for_job.return_value = None
        gh.return_value.get_workflow_job_status.return_value = status
        result = WebhookService().provision_from_task(dict(TASK_BODY))
        assert result['action'] == 'skipped'
        gc.return_value.create_runner_instance.assert_not_called()

    def test_unreadable_job_status_still_provisions(self, gh, gc):
        gc.return_value.find_live_instance_for_job.return_value = None
        gh.return_value.get_workflow_job_status.side_effect = RuntimeError('GitHub down')
        gh.return_value.get_registration_token.return_value = 'tok'
        gc.return_value.create_runner_instance.return_value = 'gcp-runner-x'
        assert WebhookService().provision_from_task(dict(TASK_BODY))['action'] == 'created'

    @pytest.mark.parametrize('change', [
        {'job_id': '4242'},
        {'template_name': ''},
        {'repo_name': 'acme/app; rm -rf /'},
        {'repo_url': 'https://evil.example/acme/app'},
        {'repo_owner_url': 'https://github.com/acme/../x y'},
    ])
    def test_malformed_payload_is_refused(self, gh, gc, change):
        with pytest.raises(ValueError):
            WebhookService().provision_from_task({**TASK_BODY, **change})
        gc.return_value.find_live_instance_for_job.assert_not_called()


class TestOidcVerification:
    def test_unconfigured_route_refuses(self):
        claims, reason = verify_google_oidc_token('Bearer x', '', [INVOKER])
        assert claims is None and 'not configured' in reason

    def test_missing_bearer_refuses(self):
        claims, reason = verify_google_oidc_token(None, MANAGER_URL, [INVOKER])
        assert claims is None and reason == 'missing bearer token'

    @patch('google.oauth2.id_token.verify_oauth2_token')
    def test_only_the_provisioner_identity_is_allowed(self, verify):
        verify.return_value = {'email': 'someone@example.com', 'email_verified': True}
        claims, reason = verify_google_oidc_token('Bearer tok', MANAGER_URL, [INVOKER])
        assert claims is None and 'not allowed' in reason
        assert verify.call_args.kwargs['audience'] == MANAGER_URL

        verify.return_value = {'email': INVOKER.upper(), 'email_verified': True}
        claims, reason = verify_google_oidc_token('Bearer tok', MANAGER_URL, [INVOKER])
        assert claims is not None and reason == ''

    @patch('google.oauth2.id_token.verify_oauth2_token', side_effect=ValueError('Token expired'))
    def test_invalid_token_refuses(self, verify):
        claims, reason = verify_google_oidc_token('Bearer tok', MANAGER_URL, [INVOKER])
        assert claims is None and 'Token expired' in reason


class TestProvisionRoute:
    def _post(self, client, body=None):
        return client.post(
            '/tasks/provision',
            data=json.dumps(TASK_BODY if body is None else body),
            content_type='application/json',
            headers={'Authorization': 'Bearer tok', 'X-CloudTasks-TaskRetryCount': '2'},
        )

    @patch('app.routes.tasks.verify_google_oidc_token', return_value=(None, 'caller x is not allowed'))
    def test_rejected_caller_learns_nothing(self, verify, client):
        response = self._post(client)
        assert response.status_code == 403
        assert response.json == {'status': 'forbidden'}

    @pytest.mark.parametrize('error,status', [
        (ZoneCapacityError('no zone'), 503),
        (QuotaExceededError('C3D_CPUS'), 503),
        (InstanceCreationError('timed out'), 500),
        (RuntimeError('boom'), 500),
        (ValueError('bad'), 200),
    ])
    @patch('app.routes.tasks.verify_google_oidc_token', return_value=({'email': INVOKER}, ''))
    @patch('app.routes.tasks.WebhookService')
    def test_errors_map_to_retry_or_done(self, service, verify, client, error, status):
        service.return_value.provision_from_task.side_effect = error
        assert self._post(client).status_code == status

    @patch('app.routes.tasks.verify_google_oidc_token', return_value=({'email': INVOKER}, ''))
    @patch('app.routes.tasks.WebhookService')
    def test_success(self, service, verify, client):
        service.return_value.provision_from_task.return_value = {
            'action': 'created', 'runner_name': 'gcp-runner-x', 'reason': ''
        }
        response = self._post(client)
        assert response.status_code == 200
        assert response.json['runner_name'] == 'gcp-runner-x'

    @patch('app.routes.tasks.verify_google_oidc_token', return_value=({'email': INVOKER}, ''))
    def test_non_object_body_is_not_retried(self, verify, client):
        assert self._post(client, body=[1, 2]).status_code == 200


class TestJobLookups:
    @patch('app.clients.gcloud_client.compute_v1')
    def test_find_live_instance_for_job_ignores_stopped_vms(self, compute, monkeypatch):
        from app.clients.gcloud_client import GCloudClient
        stopped, live = Mock(status='TERMINATED'), Mock(status='RUNNING')
        stopped.name, live.name = 'gcp-runner-a', 'gcp-runner-b'
        compute.InstancesClient.return_value.aggregated_list.return_value = [
            ('zones/us-central1-a', Mock(instances=[stopped])),
            ('zones/us-central1-b', Mock(instances=[live])),
        ]
        assert GCloudClient().find_live_instance_for_job(4242) == 'gcp-runner-b'
        request = compute.AggregatedListInstancesRequest.call_args.kwargs
        assert request['filter'] == 'labels.gha-job-id = "4242"'

    @patch('app.clients.github_client.requests.get')
    def test_job_status_and_unknown_job(self, get):
        from app.clients.github_client import GitHubClient
        client = GitHubClient()
        client.get_installation_access_token = Mock(return_value='inst')
        get.return_value = Mock(status_code=200, json=Mock(return_value={'status': 'queued'}))
        assert client.get_workflow_job_status('acme/app', 4242) == 'queued'
        assert get.call_args.args[0] == 'https://api.github.com/repos/acme/app/actions/jobs/4242'
        get.return_value = Mock(status_code=404)
        assert client.get_workflow_job_status('acme/app', 4242) is None
