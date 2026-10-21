# Get the container image from Artifact Registry
# https://registry.terraform.io/providers/hashicorp/google/latest/docs/data-sources/artifact_registry_docker_image
data "google_artifact_registry_docker_image" "container-image-github-runners-manager" {
  project       = module.project.project_id
  location      = var.region
  repository_id = module.artifact-registry-container.name
  image_name    = "app:latest" # Defined in cloudbuild-container.template.yaml
  depends_on = [
    null_resource.build-github-runners-manager-container
  ]
}

# Deploy the GitHub Actions Runners manager service on Cloud Run
# https://github.com/GoogleCloudPlatform/cloud-foundation-fabric/blob/v53.0.0/modules/cloud-run-v2/README.md
module "cloud_run_github_runners_manager" {
  source     = "git::https://github.com/GoogleCloudPlatform/cloud-foundation-fabric//modules/cloud-run-v2?ref=v53.0.0"
  project_id = module.project.project_id
  name       = local.github_runners_manager_name
  type       = "SERVICE"
  region     = var.region
  containers = {
    github-runners-manager = {
      image = data.google_artifact_registry_docker_image.container-image-github-runners-manager.self_link
      resources = {
        limits = {
          cpu    = "1000m"
          memory = "512Mi"
        }
        # With Cloud Tasks the service scales to zero and bills CPU only during requests, so a cold
        # start gets extra CPU. Without it, background provisioning needs CPU outside requests: unset
        # (null) already means always allocated here, because resource limits are set; it is pinned
        # so a provider default change cannot throttle it.
        startup_cpu_boost = var.github_runners_manager_cloud_tasks
        cpu_idle = (
          var.github_runners_manager_cloud_tasks ? true :
          var.github_runners_manager_async_provisioning ? false : null
        )
      }
      env = {
        GOOGLE_CLOUD_PROJECT = var.project_id
        GOOGLE_CLOUD_ZONE    = "${var.region}-${var.zone}"
        GITHUB_RUNNER_GROUP  = var.github_runner_group
        # Wait for each VM insert, fall back across the region's zones, then from Spot to on-demand.
        GCE_SPOT_FALLBACK_STANDARD = tostring(var.github_runners_spot_fallback_standard)
        ASYNC_PROVISIONING         = tostring(var.github_runners_manager_async_provisioning && !var.github_runners_manager_cloud_tasks)
        # Cloud Tasks handoff; empty values turn it off.
        PROVISION_QUEUE         = var.github_runners_manager_cloud_tasks ? google_cloud_tasks_queue.github-runners-provision.id : ""
        MANAGER_URL             = var.github_runners_manager_cloud_tasks ? local.github_runners_manager_url : ""
        PROVISION_INVOKER_EMAIL = var.github_runners_manager_cloud_tasks ? module.service-account-github-runners-provisioner.email : ""
      }
      env_from_key = {
        GITHUB_APP_ID = {
          # Secret may only be {secret} or
          # projects/{project}/secrets/{secret},
          # and secret may only have alphanumeric characters, hyphens, and underscores.
          # Secret must be a global secret not a regional secret!
          secret  = module.secret-manager.ids["github-app-id"]
          version = "latest"
        }
        GITHUB_INSTALLATION_ID = {
          secret  = module.secret-manager.ids["github-installation-id"]
          version = "latest"
        }
        GITHUB_PRIVATE_KEY = {
          secret  = module.secret-manager.ids["github-private-key"]
          version = "latest"
        }
        GITHUB_WEBHOOK_SECRET = {
          secret  = module.secret-manager.ids["github-webhook-secret"]
          version = "latest"
        }
      }
    }
  }
  service_config = {
    # A provisioning task waits for inserts across zones; Cloud Tasks allows it up to 30 minutes.
    timeout = "1800s"
    # Disable IAM permission check
    # There should be no requirement to pass the roles/run.invoker to the IAM block to enable public access.
    # This allows for the org policy domain restricted sharing org policy remain enabled.
    invoker_iam_disabled = true
    # Second generation Cloud Run for faster CPU.
    # The first generation with faster cold starts is still too slow for our webhook.
    gen2_execution_environment = true
    scaling = {
      # With Cloud Tasks the webhook only enqueues, so a cold start fits GitHub's timeout.
      min_instance_count = var.github_runners_manager_cloud_tasks ? 0 : var.github_runners_manager_min_instance_count
      max_instance_count = var.github_runners_manager_max_instance_count
    }
  }
  service_account_config = {
    create = false
    email  = module.service-account-cloud-run-github-runners-manager.email
  }
  deletion_protection = false
  depends_on = [
    google_secret_manager_secret_version.secret-version-default,
    time_sleep.wait_for_service_account_cloud_run
  ]
}
