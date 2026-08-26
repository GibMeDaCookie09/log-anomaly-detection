terraform {
  required_version = ">= 1.6.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State is local on purpose. A remote S3 backend needs a bucket, and the only
  # configuration that knows how to make one is this one - so bootstrapping it
  # from here is circular. For a single operator on a single workstation, local
  # state is the honest choice. For a team, create the bucket out-of-band and
  # uncomment:
  #
  # backend "s3" {
  #   bucket       = "loganomaly-tfstate-<globally-unique>"
  #   key          = "infra/terraform.tfstate"
  #   region       = "ap-south-1"
  #   encrypt      = true
  #   use_lockfile = true   # S3 native locking; no DynamoDB table needed
  # }
}
