"""
Route Cloud Tasks calls, with a Google OIDC token, to provision the runner VM for one job.
"""
import logging
import os
from flask import Blueprint, request, jsonify
from app.clients.gcloud_client import InstanceCreationError, QuotaExceededError, ZoneCapacityError
from app.services import WebhookService
from app.utils.security import verify_google_oidc_token

logger = logging.getLogger(__name__)

tasks_bp = Blueprint('tasks', __name__, url_prefix='/tasks')


@tasks_bp.route('/provision', methods=['POST'])
def provision():
    """Create the VM for the job in the task body. A non-2xx answer makes Cloud Tasks retry."""
    claims, reason = verify_google_oidc_token(
        request.headers.get('Authorization'),
        os.environ.get('MANAGER_URL', '').strip().rstrip('/'),
        [os.environ.get('PROVISION_INVOKER_EMAIL', '')],
    )
    if claims is None:
        # The reason is logged, never returned: callers learn nothing about the configuration.
        logger.error("Provisioning task rejected: %s", reason)
        return jsonify({'status': 'forbidden'}), 403

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        logger.error("Provisioning task rejected: body is not a JSON object")
        return jsonify({'status': 'error', 'message': 'invalid payload'}), 200  # not worth retrying

    job_id = payload.get('job_id')
    logger.info(
        "Provisioning task %s attempt %s for job %s, delivery_id: %s",
        request.headers.get('X-CloudTasks-TaskName', ''),
        request.headers.get('X-CloudTasks-TaskRetryCount', '0'),
        job_id,
        payload.get('delivery_id'),
    )
    try:
        result = WebhookService().provision_from_task(payload)
    except ValueError as e:
        logger.error("Provisioning task for job %s rejected: %s", job_id, e)
        return jsonify({'status': 'skipped', 'job_id': job_id}), 200
    except (ZoneCapacityError, QuotaExceededError) as e:
        # No capacity or quota right now: the queue retries with backoff while the job is queued.
        logger.warning("Provisioning task for job %s: %s; Cloud Tasks will retry", job_id, e)
        return jsonify({'status': 'retry', 'reason': 'capacity or quota'}), 503
    except InstanceCreationError as e:
        logger.error("Provisioning task for job %s failed: %s; Cloud Tasks will retry", job_id, e)
        return jsonify({'status': 'retry', 'reason': 'creation failed'}), 500
    except Exception as e:
        logger.exception("Provisioning task for job %s failed: %s", job_id, e)
        return jsonify({'status': 'retry', 'reason': 'internal error'}), 500
    return jsonify({'status': 'success', **result}), 200
