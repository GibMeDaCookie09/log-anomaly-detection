variable "aws_region" {
  description = "Region to deploy into. Free tier applies in every region; pick the nearest."
  type        = string
  default     = "ap-south-1"
}

variable "project_name" {
  description = "Name prefix for every resource. Keep it short - it ends up in bucket names."
  type        = string
  default     = "loganomaly"

  validation {
    condition     = can(regex("^[a-z0-9-]{3,24}$", var.project_name))
    error_message = "project_name must be 3-24 chars of lowercase letters, digits or hyphens (S3 naming rules)."
  }
}

variable "github_repository" {
  description = "owner/repo, used for image coordinates and resource tagging."
  type        = string
  default     = "GibMeDaCookie09/log-anomaly-detection"
}

variable "container_image" {
  description = <<-EOT
    Image the instance runs at first boot. Deployments after that are driven by
    the CD workflow, which replaces the container without re-running Terraform.
    Left empty, the instance boots with Docker installed but nothing running -
    which is the right default, since the first real image comes from a tag.
  EOT
  type        = string
  default     = ""
}

# --- Access control -----------------------------------------------------------

variable "admin_cidr" {
  description = <<-EOT
    CIDR permitted to reach SSH. Your own address, as a /32.
    Find it with:  curl -s https://checkip.amazonaws.com
  EOT
  type        = string

  validation {
    condition     = can(cidrhost(var.admin_cidr, 0))
    error_message = "admin_cidr must be valid CIDR notation, e.g. 203.0.113.7/32."
  }

  validation {
    # An open SSH port is found by scanners within minutes. If you genuinely
    # need this, say so explicitly rather than letting a default do it quietly.
    condition     = var.admin_cidr != "0.0.0.0/0"
    error_message = "Refusing to open SSH to the entire internet. Use your own /32."
  }
}

variable "api_ingress_cidrs" {
  description = <<-EOT
    CIDRs permitted to reach the API port. Defaults to admin_cidr only.
    Set to ["0.0.0.0/0"] when you want the service publicly reachable for a demo
    - the API has no authentication, so treat that as a deliberate choice.
  EOT
  type        = list(string)
  default     = []
}

variable "ssh_public_key_path" {
  description = <<-EOT
    Path to the SSH public key installed on the instance. Empty means no key
    pair is created, and you will not be able to SSH in - which also means the
    Stage 4 deploy cannot reach it.
    Generate one with:  ssh-keygen -t ed25519 -C "loganomaly-deploy"
  EOT
  type        = string
  default     = ""
}

# --- Sizing and cost ----------------------------------------------------------

variable "instance_type" {
  description = "t3.micro is free tier for 12 months (750 hrs/month). Anything larger bills."
  type        = string
  default     = "t3.micro"
}

variable "root_volume_gb" {
  description = "Root EBS size. Free tier covers 30 GB of gp2/gp3 across all instances."
  type        = number
  default     = 20

  validation {
    condition     = var.root_volume_gb >= 8 && var.root_volume_gb <= 30
    error_message = "Keep the root volume between 8 and 30 GB to stay inside the free tier."
  }
}

variable "allocate_elastic_ip" {
  description = <<-EOT
    Attach a static Elastic IP. Free while attached to a *running* instance, but
    billed hourly the moment the instance stops - a classic surprise charge.
    Left false, the instance takes an auto-assigned public IP that changes on
    every stop/start. Since the recommended workflow is terraform destroy when
    idle, the address changes between sessions either way.
  EOT
  type        = bool
  default     = false
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention. Retention is what stops stored logs billing forever."
  type        = number
  default     = 7
}

variable "s3_expire_days" {
  description = "Delete ingested logs and results after this many days. Free tier is 5 GB."
  type        = number
  default     = 30
}

variable "monthly_budget_usd" {
  description = "Budget threshold. The point is to be told immediately, not to cap spend."
  type        = number
  default     = 1
}

variable "budget_alert_email" {
  description = <<-EOT
    Address that receives the budget alert. Empty disables the budget entirely,
    which is not recommended - an unnoticed charge is the actual risk here.
  EOT
  type        = string
  default     = ""
}

variable "api_port" {
  description = "Port the container publishes."
  type        = number
  default     = 8000
}

# --- Monitoring (Stage 5) -----------------------------------------------------

variable "alarm_email" {
  description = "Address for CloudWatch alarms. Falls back to budget_alert_email when empty."
  type        = string
  default     = ""
}

variable "error_rate_threshold" {
  description = "Percent of requests returning 5xx that counts as an incident."
  type        = number
  default     = 5
}

variable "cpu_threshold" {
  description = "Sustained CPU percent that counts as an incident."
  type        = number
  default     = 80
}

variable "anomaly_alarm_threshold" {
  description = <<-EOT
    Flagged windows per 15-minute run above which the detector's own findings
    raise an alarm. The detector always returns its top-k windows, so this is a
    threshold on *how many scored above the alert threshold*, not on whether it
    returned anything.
  EOT
  type        = number
  default     = 3
}

variable "enable_grafana_read" {
  description = <<-EOT
    Grant the instance role the CloudWatch read permissions Grafana needs.
    Separate from the base policy so the read grant exists only when something
    is actually running Grafana on the host.
  EOT
  type        = bool
  default     = true
}

variable "github_immutable_repo_ref" {
  description = <<-EOT
    The owner@id/repo@id form GitHub puts in the OIDC `sub` claim when immutable
    identifiers are enabled on the account - for example
    "octocat@12345/my-repo@67890". This is not documented prominently and is not
    what the standard examples show; a trust policy written against the plain
    owner/repo form is silently rejected.

    Find yours by running the deploy once and reading the "sub =" line the
    workflow logs before it assumes the role. Leave empty if your account uses
    the plain form.
  EOT
  type        = string
  default     = "GibMeDaCookie09@148891370/log-anomaly-detection@1347566941"
}
