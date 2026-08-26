# One group, many streams: the container's stdout, the deploy script's output,
# and the detector cron each get their own stream. Retention is set explicitly -
# the default is "never expire", which is how a free-tier log group starts
# billing quietly a few months in.
resource "aws_cloudwatch_log_group" "app" {
  name              = "/${var.project_name}/app"
  retention_in_days = var.log_retention_days

  tags = {
    Name = "${var.project_name}-app-logs"
  }
}
