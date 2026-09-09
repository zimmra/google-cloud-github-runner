"""
Tests for quota handling, zones that lack the machine type, and the Spot to on-demand fallback.
"""
from unittest.mock import patch

import pytest
from google.api_core import exceptions as gapi_exceptions
from google.cloud import compute_v1

from app.clients import gcloud_client
from app.clients.gcloud_client import (
    GCloudClient,
    QuotaExceededError,
    ZoneCapacityError,
    classify_call_error,
    standard_scheduling,
)
from tests.unit.test_gcloud_client_operation_wait import (
    TEMPLATE_SELF_LINK,
    _operation_with_error,
    _recorded_exhausted_operation,
    _wrap,
)
from tests.unit.test_gcloud_client_zone_fallback import _ok_operation, _zone

QUOTA_MESSAGE = "Quota 'C3D_CPUS' exceeded.  Limit: 24.0 in region us-central1."
MISSING_TYPE_MESSAGE = (
    "Invalid value for field 'resource.machineType': 'zones/us-central1-b/machineTypes/c3d-highcpu-8'. "
    "Machine type with name 'c3d-highcpu-8' does not exist in zone 'us-central1-b'."
)


def _template(spot, max_run_duration=True):
    scheduling = compute_v1.Scheduling(
        on_host_maintenance='TERMINATE',
        automatic_restart=not spot,
    )
    if spot:
        scheduling.provisioning_model = 'SPOT'
        scheduling.preemptible = True
    if max_run_duration:
        scheduling.instance_termination_action = 'DELETE'
        scheduling.max_run_duration = compute_v1.Duration(seconds=7200)
    return compute_v1.InstanceTemplate(
        name='gcp-ubuntu-24-04-8core-12345678901234',
        self_link=TEMPLATE_SELF_LINK,
        properties=compute_v1.InstanceProperties(scheduling=scheduling),
    )


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv('GOOGLE_CLOUD_PROJECT', 'test-project')
    monkeypatch.setenv('GOOGLE_CLOUD_ZONE', 'us-central1-b')
    monkeypatch.delenv(gcloud_client.SPOT_FALLBACK_ENV, raising=False)


@pytest.fixture
def clients(env):
    with patch.object(gcloud_client.compute_v1, 'InstancesClient') as instances, \
            patch.object(gcloud_client.compute_v1, 'RegionInstanceTemplatesClient') as templates, \
            patch.object(gcloud_client.compute_v1, 'ZonesClient') as zones:
        templates.return_value.list.return_value = [_template(spot=False)]
        zones.return_value.list.return_value = [
            _zone('us-central1-a'), _zone('us-central1-b'), _zone('us-central1-c'), _zone('us-central1-f'),
        ]
        yield instances.return_value, templates.return_value


def _requests(instances):
    return [call.kwargs['request'] for call in instances.insert.call_args_list]


def _exhausted():
    return _wrap(_recorded_exhausted_operation())


def _create(client):
    return client.create_runner_instance(
        'tok', 'https://github.com/o/r', 'gcp-ubuntu-24-04-8core', instance_label='o/r'
    )


class TestQuota:
    def test_quota_in_the_operation_stops_without_trying_other_zones(self, clients):
        instances, _ = clients
        instances.insert.return_value = _wrap(
            _operation_with_error('QUOTA_EXCEEDED', QUOTA_MESSAGE, 403, 'FORBIDDEN')
        )
        with pytest.raises(QuotaExceededError, match='C3D_CPUS'):
            _create(GCloudClient())
        assert [r.zone for r in _requests(instances)] == ['us-central1-b']

    def test_quota_raised_by_the_insert_call_stops_at_once(self, clients):
        instances, _ = clients
        instances.insert.side_effect = gapi_exceptions.Forbidden(QUOTA_MESSAGE)
        with pytest.raises(QuotaExceededError):
            _create(GCloudClient())
        assert instances.insert.call_count == 1

    def test_quota_on_a_spot_template_does_not_fall_back_to_on_demand(self, clients):
        # Without a separate preemptible quota, Spot and on-demand share the family quota.
        instances, templates = clients
        templates.list.return_value = [_template(spot=True)]
        instances.insert.side_effect = gapi_exceptions.Forbidden(QUOTA_MESSAGE)
        with pytest.raises(QuotaExceededError):
            _create(GCloudClient())
        assert instances.insert.call_count == 1


class TestZoneWithoutTheMachineType:
    def test_insert_call_rejection_moves_on_to_the_next_zone(self, clients):
        instances, _ = clients
        instances.insert.side_effect = [gapi_exceptions.BadRequest(MISSING_TYPE_MESSAGE), _ok_operation()]
        assert _create(GCloudClient()).startswith('gcp-runner-')
        assert [r.zone for r in _requests(instances)] == ['us-central1-b', 'us-central1-a']

    def test_any_other_insert_call_error_still_fails_at_once(self, clients):
        instances, _ = clients
        instances.insert.side_effect = gapi_exceptions.PermissionDenied('caller lacks compute.instances.create')
        with pytest.raises(gapi_exceptions.PermissionDenied):
            _create(GCloudClient())
        assert instances.insert.call_count == 1

    @pytest.mark.parametrize('exception,kind', [
        (gapi_exceptions.Forbidden(QUOTA_MESSAGE), 'quota'),
        (gapi_exceptions.BadRequest(MISSING_TYPE_MESSAGE), 'capacity'),
        (gapi_exceptions.ServiceUnavailable('ZONE_RESOURCE_POOL_EXHAUSTED'), 'capacity'),
        (gapi_exceptions.BadRequest("Invalid value for field 'resource.name'"), 'fatal'),
    ])
    def test_classify_call_error(self, exception, kind):
        assert classify_call_error(exception) == kind


class TestSpotFallback:
    def test_spot_lands_as_spot_without_overriding_the_template(self, clients):
        instances, templates = clients
        templates.list.return_value = [_template(spot=True)]
        instances.insert.return_value = _ok_operation()
        _create(GCloudClient())
        (request,) = _requests(instances)
        assert request.instance_resource.labels['gha-provisioning'] == 'spot'
        assert 'scheduling' not in request.instance_resource

    def test_spot_stockout_everywhere_retries_every_zone_on_demand(self, clients):
        instances, templates = clients
        templates.list.return_value = [_template(spot=True)]
        instances.insert.side_effect = [_exhausted() for _ in range(4)] + [_exhausted(), _ok_operation()]
        assert _create(GCloudClient()).startswith('gcp-runner-')

        requests = _requests(instances)
        zones = ['us-central1-b', 'us-central1-a', 'us-central1-c', 'us-central1-f']
        assert [r.zone for r in requests] == zones + zones[:2]
        assert all('scheduling' not in r.instance_resource for r in requests[:4])
        landed = requests[-1].instance_resource
        assert landed.labels['gha-provisioning'] == 'standard'
        assert landed.labels['gha-zone'] == 'us-central1-a'
        assert landed.scheduling.provisioning_model == 'STANDARD'
        assert landed.scheduling.preemptible is False
        # Everything else in the template's scheduling is kept.
        assert landed.scheduling.max_run_duration.seconds == 7200
        assert landed.scheduling.instance_termination_action == 'DELETE'
        assert landed.scheduling.on_host_maintenance == 'TERMINATE'

    def test_fallback_can_be_turned_off(self, clients, monkeypatch):
        monkeypatch.setenv(gcloud_client.SPOT_FALLBACK_ENV, 'false')
        instances, templates = clients
        templates.list.return_value = [_template(spot=True)]
        instances.insert.side_effect = [_exhausted() for _ in range(4)]
        with pytest.raises(ZoneCapacityError):
            _create(GCloudClient())
        assert instances.insert.call_count == 4

    def test_on_demand_template_tries_each_zone_once(self, clients):
        instances, _ = clients
        instances.insert.side_effect = [_exhausted() for _ in range(4)]
        with pytest.raises(ZoneCapacityError) as raised:
            _create(GCloudClient())
        assert instances.insert.call_count == 4
        assert 'us-central1-f' in str(raised.value)

    def test_both_rungs_exhausted_reports_every_attempt(self, clients):
        instances, templates = clients
        templates.list.return_value = [_template(spot=True)]
        instances.insert.side_effect = [_exhausted() for _ in range(8)]
        with pytest.raises(ZoneCapacityError) as raised:
            _create(GCloudClient())
        assert instances.insert.call_count == 8
        assert str(raised.value).count('us-central1-b') == 2


class TestStandardScheduling:
    def test_termination_action_is_dropped_without_a_max_run_duration(self):
        template = _template(spot=True, max_run_duration=False)
        template.properties.scheduling.instance_termination_action = 'DELETE'
        scheduling = standard_scheduling(template)
        assert scheduling.provisioning_model == 'STANDARD'
        assert 'instance_termination_action' not in scheduling

    def test_the_template_itself_is_not_modified(self):
        template = _template(spot=True)
        standard_scheduling(template)
        assert template.properties.scheduling.provisioning_model == 'SPOT'
