# ------------------------------------------------------------------------------
# Stage 5: turning logs into metrics, and metrics into alarms.
#
# The application emits one JSON line per request. These filters parse that line
# into CloudWatch metrics without an agent and without the application knowing
# CloudWatch exists - the app's only contract is the log format asserted in
# tests/test_api.py.
# ------------------------------------------------------------------------------

locals {
  metric_namespace = "${var.project_name}/api"
  alarm_email      = var.alarm_email != "" ? var.alarm_email : var.budget_alert_email
}

resource "aws_cloudwatch_log_metric_filter" "requests" {
  name           = "${var.project_name}-requests"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.event = \"request\" }"

  metric_transformation {
    name      = "RequestCount"
    namespace = local.metric_namespace
    value     = "1"
    unit      = "Count"
    # Without this, periods with no traffic report *no data* rather than zero,
    # and a rate calculated against them is undefined instead of 0%.
    default_value = "0"
  }
}

resource "aws_cloudwatch_log_metric_filter" "server_errors" {
  name           = "${var.project_name}-server-errors"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.event = \"request\" && $.status >= 500 }"

  metric_transformation {
    name          = "ServerErrors"
    namespace     = local.metric_namespace
    value         = "1"
    unit          = "Count"
    default_value = "0"
  }
}

resource "aws_cloudwatch_log_metric_filter" "latency" {
  name           = "${var.project_name}-latency"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.event = \"request\" }"

  metric_transformation {
    name      = "LatencyMs"
    namespace = local.metric_namespace
    # The metric value is the field itself, not a count.
    value = "$.duration_ms"
    unit  = "Milliseconds"
    # Deliberately no default_value: filling idle periods with 0 ms would drag
    # every latency average toward zero and hide real regressions.
  }
}

# ------------------------------------------------------------------------------

resource "aws_sns_topic" "alerts" {
  name = "${var.project_name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  count = local.alarm_email != "" ? 1 : 0

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = local.alarm_email
  # AWS sends a confirmation link; the subscription is inactive until clicked.
  # Terraform cannot do that for you and will show the subscription as pending.
}

# ------------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "api_5xx_rate" {
  alarm_name        = "${var.project_name}-api-5xx-rate"
  alarm_description = "Server error rate above ${var.error_rate_threshold}% over 5 minutes."

  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  threshold           = var.error_rate_threshold
  # No traffic is not an outage. Without this the alarm goes INSUFFICIENT_DATA
  # every quiet night and trains you to ignore it.
  treat_missing_data = "notBreaching"

  metric_query {
    id          = "error_rate"
    label       = "5xx rate (%)"
    expression  = "100 * (errors / IF(requests > 0, requests, 1))"
    return_data = true
  }

  metric_query {
    id = "errors"
    metric {
      metric_name = "ServerErrors"
      namespace   = local.metric_namespace
      period      = 300
      stat        = "Sum"
    }
  }

  metric_query {
    id = "requests"
    metric {
      metric_name = "RequestCount"
      namespace   = local.metric_namespace
      period      = 300
      stat        = "Sum"
    }
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  depends_on = [
    aws_cloudwatch_log_metric_filter.requests,
    aws_cloudwatch_log_metric_filter.server_errors,
  ]
}

resource "aws_cloudwatch_metric_alarm" "instance_cpu" {
  alarm_name        = "${var.project_name}-instance-cpu"
  alarm_description = "Instance CPU sustained above ${var.cpu_threshold}%."

  # A default EC2 metric - no agent, no custom metric, no cost.
  namespace   = "AWS/EC2"
  metric_name = "CPUUtilization"
  statistic   = "Average"
  period      = 300
  # Two periods, not one: a t3.micro briefly pegs the CPU on every deploy while
  # the pipeline fits, and paging on that would be pure noise.
  evaluation_periods  = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.cpu_threshold

  dimensions = {
    InstanceId = aws_instance.app.id
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "instance_status" {
  alarm_name        = "${var.project_name}-instance-status"
  alarm_description = "EC2 status check failing - the host itself is unhealthy."

  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0

  dimensions = {
    InstanceId = aws_instance.app.id
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
}

# The loop, closed: the detector's own output is a metric, and an unusual number
# of flagged windows raises an alarm like any other operational signal.
resource "aws_cloudwatch_metric_alarm" "anomalies_detected" {
  alarm_name        = "${var.project_name}-anomalies-detected"
  alarm_description = "The service's own logs are being flagged as anomalous by the detector."

  namespace           = local.metric_namespace
  metric_name         = "AnomaliesDetected"
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.anomaly_alarm_threshold
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
}
