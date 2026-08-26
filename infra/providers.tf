provider "aws" {
  region = var.aws_region

  # Applied to every taggable resource. Without this, cost attribution in the
  # billing console is guesswork, and you cannot tell which of your projects is
  # the one spending money.
  default_tags {
    tags = {
      Project   = var.project_name
      ManagedBy = "terraform"
      Repo      = var.github_repository
    }
  }
}
