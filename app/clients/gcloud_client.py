"""
Google Cloud Client for managing GCE instances.
"""
import concurrent.futures
import logging
import os
import re
import uuid
import shlex
import time
import google.cloud.compute_v1 as compute_v1
from google.api_core import exceptions as gapi_exceptions

logger = logging.getLogger(__name__)

# How long to wait for an instances.insert operation before giving up (seconds).
DEFAULT_INSERT_TIMEOUT_SECONDS = 120
# How long the list of zones in the region is cached (seconds).
ZONE_CACHE_SECONDS = 600
# Instance label that records the zone the VM landed in after zone fallback.
ZONE_LABEL = 'gha-zone'
# Instance label that records whether the VM runs as Spot or on demand (standard).
PROVISIONING_LABEL = 'gha-provisioning'
# Instance label that records the workflow job a VM was created for.
JOB_LABEL = 'gha-job-id'
# Instance statuses of a VM that is, or is about to be, a runner.
LIVE_STATUSES = ('PROVISIONING', 'STAGING', 'RUNNING')

# Operation error codes that mean "this zone cannot serve the request right now".
# https://cloud.google.com/compute/docs/troubleshooting/troubleshooting-vm-creation
RETRY_ELSEWHERE_CODE_PREFIXES = (
    'ZONE_RESOURCE_POOL_EXHAUSTED',
)
# Message fragments that indicate a stockout even when the code is generic.
RETRY_ELSEWHERE_MESSAGE_MARKERS = (
    'stockout',
    'does not have enough resources',
    'currently unavailable',
)
# Quota is regional (or project-wide), so another zone of the region cannot help.
QUOTA_CODE_PREFIXES = (
    'QUOTA_EXCEEDED',
)
QUOTA_MESSAGE_PATTERN = re.compile(r"quota '[^']+' exceeded|quota_exceeded", re.IGNORECASE)
# Errors the insert call itself raises when a zone does not offer the machine type (or the
# template's disk type): skip that zone, the others of the region may offer it.
ZONE_UNSUPPORTED_MESSAGE_MARKERS = (
    'does not exist in zone',
    'not available in zone',
    'is not supported in zone',
    'not supported in this zone',
)
# Set GCE_SPOT_FALLBACK_STANDARD=false to never fall back from Spot to on-demand VMs.
SPOT_FALLBACK_ENV = 'GCE_SPOT_FALLBACK_STANDARD'


class InstanceCreationError(Exception):
    """The instances.insert operation did not succeed. Never swallowed."""


class ZoneCapacityError(InstanceCreationError):
    """The zone cannot serve the request (stockout, or it lacks the machine type); another zone may."""

    def __init__(self, message, code=None, zone=None):
        super().__init__(message)
        self.code = code
        self.zone = zone


class QuotaExceededError(InstanceCreationError):
    """The project or region is out of quota. No other zone or provisioning model can help."""


def is_quota_error(errors):
    """True when any error of a finished Compute operation is a quota error."""
    for error in errors or []:
        code = str(getattr(error, 'code', '') or '')
        message = str(getattr(error, 'message', '') or '')
        if code.startswith(QUOTA_CODE_PREFIXES) or QUOTA_MESSAGE_PATTERN.search(message):
            return True
    return False


def classify_call_error(exception):
    """
    Classify an exception raised by the insert call itself (before any operation exists).

    Returns:
        str: 'quota', 'capacity' (stockout or the zone lacks the machine type) or 'fatal'.
    """
    text = str(exception)
    lowered = text.lower()
    if 'QUOTA_EXCEEDED' in text or QUOTA_MESSAGE_PATTERN.search(text):
        return 'quota'
    if any(prefix in text for prefix in RETRY_ELSEWHERE_CODE_PREFIXES) \
            or any(marker in lowered for marker in RETRY_ELSEWHERE_MESSAGE_MARKERS) \
            or any(marker in lowered for marker in ZONE_UNSUPPORTED_MESSAGE_MARKERS):
        return 'capacity'
    return 'fatal'


def is_spot(template):
    """True when an instance template provisions Spot VMs."""
    try:
        model = template.properties.scheduling.provisioning_model
    except AttributeError:
        return False
    return isinstance(model, str) and model.upper() == 'SPOT'


def standard_scheduling(template):
    """
    The template's scheduling with the provisioning model switched from Spot to on-demand.

    The whole scheduling block is copied so every other setting (max run duration, host
    maintenance) is kept; the instance's scheduling replaces the template's on insert.
    The termination action is kept only with a max run duration, its only use on demand.
    """
    source = template.properties.scheduling
    scheduling = compute_v1.Scheduling.deserialize(compute_v1.Scheduling.serialize(source))
    scheduling.provisioning_model = 'STANDARD'
    scheduling.preemptible = False
    if 'max_run_duration' not in scheduling and 'instance_termination_action' in scheduling:
        del scheduling.instance_termination_action
    return scheduling


def classify_operation_errors(errors):
    """
    Classify the ``error.errors`` list of a finished Compute operation.

    Args:
        errors: iterable of compute_v1.Errors (or anything with .code/.message/.error_details).

    Returns:
        tuple(bool, str): (retry_elsewhere, human readable summary "CODE: message; ...").
        Quota errors are not retry_elsewhere: see is_quota_error.
    """
    retry_elsewhere = False
    parts = []
    for error in errors or []:
        code = str(getattr(error, 'code', '') or '')
        message = str(getattr(error, 'message', '') or '')
        reason = ''
        zone = ''
        for detail in getattr(error, 'error_details', None) or []:
            error_info = getattr(detail, 'error_info', None)
            if error_info is not None:
                reason = reason or str(getattr(error_info, 'reason', '') or '')
                metadatas = getattr(error_info, 'metadatas', None) or {}
                zone = zone or str(metadatas.get('zone', '') if hasattr(metadatas, 'get') else '')
        summary = f"{code}: {message}"
        if reason:
            summary += f" (reason={reason}"
            summary += f", zone={zone})" if zone else ")"
        parts.append(summary)
        haystack = f"{message} {reason}".lower()
        if is_quota_error([error]):
            continue
        if code.startswith(RETRY_ELSEWHERE_CODE_PREFIXES) or reason.lower() == 'stockout' \
                or any(marker in haystack for marker in RETRY_ELSEWHERE_MESSAGE_MARKERS):
            retry_elsewhere = True
    return retry_elsewhere, '; '.join(parts)


class GCloudClient:
    """Client for interacting with Google Cloud Compute Engine API."""

    def __init__(self):
        """Initialize GCloudClient with project and zone configuration."""
        self.project_id = os.environ.get('GOOGLE_CLOUD_PROJECT')
        self.zone = os.environ.get('GOOGLE_CLOUD_ZONE', 'us-central1-a')
        self.github_runner_group = os.environ.get('GITHUB_RUNNER_GROUP', '').strip()
        self.region = '-'.join(self.zone.split('-')[:-1])
        self.insert_timeout = int(os.environ.get('GCE_INSERT_TIMEOUT_SECONDS', DEFAULT_INSERT_TIMEOUT_SECONDS))

        if not self.project_id:
            logger.warning("GOOGLE_CLOUD_PROJECT not set. GCloudClient will not work correctly.")

        # https://docs.cloud.google.com/python/docs/reference/compute/latest/google.cloud.compute_v1.services.instances.InstancesClient
        self.instance_client = compute_v1.InstancesClient()
        # Create a RegionInstanceTemplatesClient for retrieving templates in a specific region
        # https://docs.cloud.google.com/python/docs/reference/compute/latest/google.cloud.compute_v1.services.region_instance_templates
        self.instance_templates_client = compute_v1.RegionInstanceTemplatesClient()
        # Zones of the region are listed lazily and cached for ZONE_CACHE_SECONDS.
        self._zones_client = None
        self._zones_cache = None
        self._zones_cache_time = 0.0

    def candidate_zones(self):
        """
        Zones of the template region to try, in a stable order.

        The configured zone (GOOGLE_CLOUD_ZONE) comes first, followed by the other UP zones of
        the same region sorted by name. The list comes from the Compute API, not a hardcoded table.
        On any listing error only the configured zone is returned (and the error is logged).

        Returns:
            list[str]: zone names, e.g. ['us-central1-b', 'us-central1-a', 'us-central1-c'].
        """
        now = time.monotonic()
        if self._zones_cache is not None and now - self._zones_cache_time < ZONE_CACHE_SECONDS:
            return list(self._zones_cache)
        zones = [self.zone]
        try:
            if self._zones_client is None:
                # https://docs.cloud.google.com/python/docs/reference/compute/latest/google.cloud.compute_v1.services.zones
                self._zones_client = compute_v1.ZonesClient()
            others = []
            for zone in self._zones_client.list(project=self.project_id):
                region = str(getattr(zone, 'region', '') or '').rstrip('/').split('/')[-1]
                status = str(getattr(zone, 'status', '') or '')
                if region != self.region or status.upper() != 'UP' or zone.name == self.zone:
                    continue
                others.append(zone.name)
            zones.extend(sorted(others))
            self._zones_cache = list(zones)
            self._zones_cache_time = now
        except Exception as e:
            logger.warning(
                "Could not list zones of region %s (%s); using configured zone %s only",
                self.region,
                e,
                self.zone,
            )
        return zones

    def _get_template_name(self, template_name):
        """
        Find a matching instance template by name prefix.

        Args:
            template_name (str): The name prefix to search for.

        Returns:
            google.cloud.compute_v1.InstanceTemplate or None: The matching template resource.
        """
        # Replace dots with dashes for template name, so gcp-ubuntu-24.04 matches gcp-ubuntu-24-04
        prefix = template_name.replace('.', '-')
        # logger.info(f"Prefix: {prefix}")
        # Create regex pattern: prefix followed by dash, at least 12 digits, and optional alphanumeric characters
        pattern = re.compile(f"^{re.escape(prefix)}-\\d{{14,}}[a-z0-9]*$")
        try:
            # List all templates to find one that matches the pattern
            for template in self.instance_templates_client.list(project=self.project_id, region=self.region):
                # logger.info(f"Template: {template.name}")
                if pattern.match(template.name):
                    return template
            return None
        except Exception:
            return None

    def create_runner_instance(
        self,
        registration_token,
        repo_url,
        template_name,
        instance_label=None,
        delivery_id=None,
        job_id=None,
    ):
        """
        Create a new GCE instance for a GitHub Actions runner.

        Args:
            registration_token (str): The GitHub Actions runner registration token.
            repo_url (str): The URL of the repository or organization.
            template_name (str): The name of the instance template to use.
            instance_label (str): Label to add to the Instance for Cost Tracking.
            delivery_id (str): The GitHub webhook delivery ID for log correlation.

        Returns:
            str: The name of the created instance.
        """
        instance_template_resource = self._get_template_name(template_name)
        if instance_template_resource:
            logger.info(
                "Found matching instance template: %s, delivery_id: %s",
                instance_template_resource.name,
                delivery_id,
            )
        else:
            logger.warning(
                "No matching instance template found for label '%s' in region %s. "
                "Skipping instance creation. delivery_id: %s",
                template_name,
                self.region,
                delivery_id,
            )
            return None

        # Name must start with a lowercase letter followed by up to 62 lowercase letters,
        # numbers, or hyphens, and cannot end with a hyphen.
        instance_uuid = uuid.uuid4().hex[:16]
        if instance_template_resource.name.startswith("dependabot"):
            instance_name = f"gcp-runner-dependabot-{instance_uuid}"
        else:
            instance_name = f"gcp-runner-{instance_uuid}"

        logger.info(
            "Creating GCE instance %s with template %s, delivery_id: %s",
            instance_name,
            instance_template_resource.self_link,
            delivery_id,
        )

        # Set instance name
        instance_resource = compute_v1.Instance()  # google.cloud.compute_v1.types.Instance
        instance_resource.name = instance_name

        labels = {}
        if instance_label is not None:
            owner, repo = instance_label.split("/")
            labels = {
                "gha-owner": owner.lower(),
                "gha-repo": repo.lower(),
                "gha-runner": template_name
            }
        if job_id is not None:
            # Lets a retried provisioning task see that this job already has a VM.
            labels[JOB_LABEL] = str(int(job_id))

        # Set metadata (startup script) - use shlex.quote to prevent command injection
        runner_group_flag = ""
        if self.github_runner_group:
            runner_group_flag = f" --runnergroup {shlex.quote(self.github_runner_group)}"

        startup_script = (
            "cd /actions-runner && "
            f"sudo -u runner ./config.sh --url {shlex.quote(repo_url)} "
            f"--token {shlex.quote(registration_token)} "
            f"--name {shlex.quote(instance_name)} "
            f"--labels {shlex.quote(template_name)} "
            f"{runner_group_flag} "
            "--ephemeral "
            "--unattended "
            "--no-default-labels "
            "--disableupdate && "
            "sudo -u runner ./run.sh"
        )
        metadata = compute_v1.Metadata()
        metadata.items = [
            compute_v1.Items(key="startup-script", value=startup_script),
            compute_v1.Items(key="vmDnsSetting", value="ZonalOnly"),
            compute_v1.Items(key="block-project-ssh-keys", value="true"),
        ]
        instance_resource.metadata = metadata

        # Spot templates get a second rung: the same zones again as on-demand VMs, so a Spot
        # stockout across the region still yields a runner.
        rungs = [('spot' if is_spot(instance_template_resource) else 'standard', None)]
        if rungs[0][0] == 'spot' and os.environ.get(SPOT_FALLBACK_ENV, 'true').lower() != 'false':
            rungs.append(('standard', standard_scheduling(instance_template_resource)))

        capacity_failures = []
        for provisioning, scheduling in rungs:
            if scheduling is not None:
                logger.warning(
                    "No zone in region %s has Spot capacity for %s; trying on-demand, delivery_id: %s",
                    self.region,
                    instance_name,
                    delivery_id,
                )
                instance_resource.scheduling = scheduling
            labels[PROVISIONING_LABEL] = provisioning
            try:
                zone = self._insert_across_zones(
                    instance_resource, instance_template_resource, labels, instance_name, delivery_id
                )
            except ZoneCapacityError as e:
                capacity_failures.extend(getattr(e, 'failures', [e]))
                continue
            logger.info(
                "Instance %s created in zone %s (%s) with template %s, delivery_id: %s",
                instance_name,
                zone,
                provisioning,
                instance_template_resource.name,
                delivery_id,
            )
            return instance_name

        tried = ', '.join(f"{e.zone} ({e.code})" for e in capacity_failures)
        logger.error(
            "No zone in region %s could create instance %s; tried %s, delivery_id: %s",
            self.region,
            instance_name,
            tried,
            delivery_id,
        )
        last = capacity_failures[-1]
        raise ZoneCapacityError(
            f"No zone in region {self.region} could create {instance_name}; tried {tried}",
            code=last.code,
            zone=last.zone,
        ) from last

    def _insert_across_zones(self, instance_resource, template, labels, instance_name, delivery_id=None):
        """
        Insert the instance in the configured zone, then in the region's other zones.

        Templates are regional, so every zone of the region is a valid target. A zone that is
        out of capacity, or does not offer the machine type, moves on to the next zone. Quota
        and every other error fail at once.

        Returns:
            str: the zone the instance was created in.

        Raises:
            ZoneCapacityError: no zone could serve the request; ``failures`` lists each zone's.
            QuotaExceededError: out of quota; no zone was tried after it.
            InstanceCreationError / Exception: any other failure.
        """
        failures = []
        for zone in self.candidate_zones():
            labels[ZONE_LABEL] = zone
            instance_resource.labels = dict(labels)
            # https://docs.cloud.google.com/python/docs/reference/compute/latest/google.cloud.compute_v1.types.InsertInstanceRequest
            request = compute_v1.InsertInstanceRequest(
                project=self.project_id,
                zone=zone,
                instance_resource=instance_resource,
                source_instance_template=template.self_link
            )
            try:
                # https://docs.cloud.google.com/compute/docs/reference/rest/v1/instances/insert
                operation = self.instance_client.insert(request=request)
                logger.info(
                    "Instance creation operation started: %s, zone: %s, delivery_id: %s",
                    operation.name,
                    zone,
                    delivery_id,
                )
            except Exception as e:
                kind = classify_call_error(e)
                if kind == 'capacity':
                    logger.warning(
                        "Zone %s cannot create instance %s: %s, delivery_id: %s",
                        zone,
                        instance_name,
                        e,
                        delivery_id,
                    )
                    failures.append(ZoneCapacityError(str(e), code=type(e).__name__, zone=zone))
                    continue
                logger.error(
                    "Failed to create instance %s in zone %s: %s, delivery_id: %s",
                    instance_name,
                    zone,
                    e,
                    delivery_id,
                )
                if kind == 'quota':
                    raise QuotaExceededError(
                        f"Out of quota creating {instance_name} in region {self.region}: {e}"
                    ) from e
                raise

            # Wait for the operation: capacity errors (stockout, quota) are operation-level
            # errors, so returning here would report success for a VM that never existed.
            try:
                self._wait_for_insert(operation, instance_name, zone, delivery_id=delivery_id)
            except ZoneCapacityError as e:
                failures.append(e)
                continue
            return zone

        error = ZoneCapacityError(
            f"No zone in region {self.region} could create {instance_name}",
            code=failures[-1].code if failures else None,
            zone=failures[-1].zone if failures else None,
        )
        error.failures = failures
        raise error

    def _wait_for_insert(self, operation, instance_name, zone, delivery_id=None):
        """
        Block until the insert operation finishes and raise if it did not succeed.

        Raises:
            ZoneCapacityError: the zone is out of capacity; another zone may work.
            QuotaExceededError: out of quota; no other zone can help.
            InstanceCreationError: any other failure, including a timed-out wait.
        """
        try:
            operation.result(timeout=self.insert_timeout)
        except concurrent.futures.TimeoutError:
            # The operation may still complete; do NOT retry elsewhere or we could double-provision.
            message = (
                f"Instance creation for {instance_name} in zone {zone} timed out after "
                f"{self.insert_timeout}s (operation {getattr(operation, 'name', '?')}); not retrying"
            )
            logger.error("%s, delivery_id: %s", message, delivery_id)
            raise InstanceCreationError(message)
        except gapi_exceptions.GoogleAPICallError as e:
            errors = self._operation_errors(operation)
            retry_elsewhere, summary = classify_operation_errors(errors)
            if not summary:
                summary = str(e)
            code = str(getattr(errors[0], 'code', '') or '') if errors else ''
            if is_quota_error(errors):
                logger.error(
                    "Instance creation for %s in zone %s is out of quota: %s, delivery_id: %s",
                    instance_name,
                    zone,
                    summary,
                    delivery_id,
                )
                raise QuotaExceededError(
                    f"Out of quota creating {instance_name} in zone {zone}: {summary}"
                ) from e
            if retry_elsewhere:
                logger.warning(
                    "Instance creation for %s in zone %s failed with a capacity error: %s, delivery_id: %s",
                    instance_name,
                    zone,
                    summary,
                    delivery_id,
                )
                raise ZoneCapacityError(
                    f"Zone {zone} has no capacity for {instance_name}: {summary}", code=code, zone=zone
                ) from e
            logger.error(
                "Instance creation for %s in zone %s failed: %s, delivery_id: %s",
                instance_name,
                zone,
                summary,
                delivery_id,
            )
            raise InstanceCreationError(
                f"Instance creation for {instance_name} in zone {zone} failed: {summary}"
            ) from e

        # Defensive: a DONE operation that carries errors but did not raise is still a failure.
        errors = self._operation_errors(operation)
        if errors:
            _, summary = classify_operation_errors(errors)
            logger.error(
                "Instance creation for %s in zone %s finished with errors: %s, delivery_id: %s",
                instance_name,
                zone,
                summary,
                delivery_id,
            )
            raise InstanceCreationError(
                f"Instance creation for {instance_name} in zone {zone} finished with errors: {summary}"
            )

    @staticmethod
    def _operation_errors(operation):
        """Return the list of compute_v1.Errors attached to an operation (empty if none)."""
        try:
            error = getattr(operation, 'error', None)
            errors = list(getattr(error, 'errors', None) or [])
        except Exception:
            return []
        # Guard against MagicMock-like objects in tests returning non-Errors items.
        return [e for e in errors if isinstance(getattr(e, 'code', None), str)]

    def find_instance_zone(self, instance_name, delivery_id=None):
        """
        Locate the zone an instance lives in, searching every zone of the project.

        Returns:
            str or None: the zone name, or None when no instance with that name exists.

        Raises:
            Exception: if the aggregated list call itself fails.
        """
        # https://docs.cloud.google.com/compute/docs/reference/rest/v1/instances/aggregatedList
        request = compute_v1.AggregatedListInstancesRequest(
            project=self.project_id,
            filter=f'name = "{instance_name}"',
        )
        for zone_key, scoped_list in self.instance_client.aggregated_list(request=request):
            for instance in getattr(scoped_list, 'instances', None) or []:
                if instance.name == instance_name:
                    zone = str(getattr(instance, 'zone', '') or '').rstrip('/').split('/')[-1]
                    return zone or str(zone_key).split('/')[-1]
        return None

    def find_live_instance_for_job(self, job_id):
        """
        The name of a live VM already created for ``job_id``, in any zone, or None.

        Raises:
            Exception: if the aggregated list call itself fails.
        """
        # https://docs.cloud.google.com/compute/docs/reference/rest/v1/instances/aggregatedList
        request = compute_v1.AggregatedListInstancesRequest(
            project=self.project_id,
            filter=f'labels.{JOB_LABEL} = "{int(job_id)}"',
        )
        for _, scoped_list in self.instance_client.aggregated_list(request=request):
            for instance in getattr(scoped_list, 'instances', None) or []:
                if str(getattr(instance, 'status', '')) in LIVE_STATUSES:
                    return instance.name
        return None

    def delete_runner_instance(self, instance_name, delivery_id=None, zone=None):
        """
        Delete a GCE instance, wherever in the project it lives.

        Args:
            instance_name (str): The name of the instance to delete.
            delivery_id (str): The GitHub webhook delivery ID for log correlation.
            zone (str): Zone of the instance if already known; otherwise it is looked up.

        Returns:
            str or None: the zone the delete was issued in, or None if the instance was not found.
        """
        if zone is None:
            try:
                zone = self.find_instance_zone(instance_name, delivery_id=delivery_id)
            except Exception as e:
                logger.warning(
                    "Could not look up zone of instance %s (%s); trying configured zone %s, delivery_id: %s",
                    instance_name,
                    e,
                    self.zone,
                    delivery_id,
                )
                zone = self.zone
            if zone is None:
                logger.warning(
                    "Instance %s not found in any zone of project %s; nothing to delete, delivery_id: %s",
                    instance_name,
                    self.project_id,
                    delivery_id,
                )
                return None

        logger.info(
            "Deleting GCE instance %s in zone %s, delivery_id: %s", instance_name, zone, delivery_id
        )
        try:
            operation = self.instance_client.delete(
                project=self.project_id,
                zone=zone,
                instance=instance_name
            )
            logger.info(
                "Instance deletion operation started: %s, zone: %s, delivery_id: %s",
                operation.name,
                zone,
                delivery_id,
            )
            return zone
        except Exception as e:
            logger.error(
                "Failed to delete instance %s in zone %s: %s, delivery_id: %s",
                instance_name,
                zone,
                e,
                delivery_id,
            )
            raise
