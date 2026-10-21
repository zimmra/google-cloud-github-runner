# Cloud Tasks handoff: the webhook enqueues one task per queued job and the queue calls the
# manager's /tasks/provision route, which creates the runner VM. The webhook then answers GitHub
# at once, capacity and quota errors are retried with backoff while the job is still queued, and
# the manager can scale to zero with CPU billed only during requests.

locals {
  github_runners_manager_name = "github-runners-manager-${local.region_shortnames[var.region]}"
  # Cloud Run's deterministic URL, known before the service exists: the service passes it to its
  # own tasks as their target and as the audience of their OIDC token.
  github_runners_manager_url = "https://${local.github_runners_manager_name}-${module.project.number}.${var.region}.run.app"
}

# Identity of the OIDC token each task carries; the /tasks/provision route accepts only this one.
# https://github.com/GoogleCloudPlatform/cloud-foundation-fabric/blob/v53.0.0/modules/iam-service-account/README.md
module "service-account-github-runners-provisioner" {
  source       = "git::https://github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/iam-service-account?ref=v53.0.0"
  project_id   = module.project.project_id
  name         = "github-runners-provisioner"
  display_name = "Cloud Tasks - GitHub Actions Runners provisioning (Terraform managed)"
  iam = {
    # The manager names this account as the token identity when it creates a task.
    "roles/iam.serviceAccountUser" = [
      module.service-account-cloud-run-github-runners-manager.iam_email
    ]
  }
}

# https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/cloud_tasks_queue
resource "google_cloud_tasks_queue" "github-runners-provision" {
  project  = module.project.project_id
  name     = "github-runners-provision-${local.region_shortnames[var.region]}"
  location = var.region

  rate_limits {
    max_dispatches_per_second = 5
    max_concurrent_dispatches = var.github_runners_provision_max_concurrent
  }

  # Retry until the job is no longer queued (each attempt checks) or the retry window ends.
  retry_config {
    max_attempts       = -1
    max_retry_duration = "${var.github_runners_provision_retry_seconds}s"
    min_backoff        = "15s"
    max_backoff        = "120s"
    max_doublings      = 3
  }

  depends_on = [module.project]
}

resource "google_cloud_tasks_queue_iam_member" "github-runners-provision-enqueuer" {
  project  = google_cloud_tasks_queue.github-runners-provision.project
  location = google_cloud_tasks_queue.github-runners-provision.location
  name     = google_cloud_tasks_queue.github-runners-provision.name
  role     = "roles/cloudtasks.enqueuer"
  member   = module.service-account-cloud-run-github-runners-manager.iam_email
}
