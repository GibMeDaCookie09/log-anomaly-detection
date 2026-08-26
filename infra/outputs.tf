locals {
  # one() yields null when the EIP is not created; coalesce then falls back to
  # the auto-assigned address. Indexing [0] on a count=0 resource would error.
  public_ip = coalesce(one(aws_eip.app[*].public_ip), aws_instance.app.public_ip)
}

output "instance_id" {
  description = "EC2 instance ID."
  value       = aws_instance.app.id
}

output "public_ip" {
  description = "Public address of the host. Changes on every apply unless allocate_elastic_ip is set."
  value       = local.public_ip
}

output "api_url" {
  description = "Base URL of the service."
  value       = "http://${local.public_ip}:${var.api_port}"
}

output "health_url" {
  description = "The endpoint CI, the deploy gate and any uptime check should use."
  value       = "http://${local.public_ip}:${var.api_port}/health"
}

output "ssh_command" {
  description = "Ready-to-paste SSH command, or a note if no key pair was configured."
  value = (
    var.ssh_public_key_path != ""
    ? "ssh ec2-user@${local.public_ip}"
    : "no key pair configured - set ssh_public_key_path to enable SSH and CD"
  )
}

output "s3_bucket" {
  description = "Log ingestion and results bucket."
  value       = aws_s3_bucket.logs.bucket
}

output "log_group" {
  description = "CloudWatch log group receiving container stdout."
  value       = aws_cloudwatch_log_group.app.name
}

output "aws_region" {
  description = "Region everything was created in."
  value       = var.aws_region
}

# Consumed by the Stage 4 deploy workflow, which needs the host and the image
# coordinates in one place rather than duplicated in GitHub secrets.
output "deploy_target" {
  description = "Everything the CD workflow needs to reach this host."
  value = {
    host      = local.public_ip
    user      = "ec2-user"
    api_port  = var.api_port
    log_group = aws_cloudwatch_log_group.app.name
    s3_bucket = aws_s3_bucket.logs.bucket
    region    = var.aws_region
  }
}

output "github_actions_role_arn" {
  description = "Set this as the AWS_ROLE_ARN repository variable so the deploy job can assume it."
  value       = aws_iam_role.github_deploy.arn
}

output "security_group_id" {
  description = "The group the deploy job opens and closes SSH on."
  value       = aws_security_group.instance.id
}
