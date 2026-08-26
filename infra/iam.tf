# Prefixes the instance is allowed to touch. Naming them here rather than
# granting the whole bucket is the difference between "scoped to the bucket" and
# "scoped to what it actually does".
locals {
  s3_prefixes = ["logs", "results"]
}

data "aws_iam_policy_document" "assume_ec2" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name_prefix        = "${var.project_name}-instance-"
  assume_role_policy = data.aws_iam_policy_document.assume_ec2.json
  description        = "Role assumed by the ${var.project_name} EC2 instance."
}

# ------------------------------------------------------------------------------
# Least privilege, concretely.
#
# No wildcards on resources. No AWS managed policies - AmazonS3FullAccess would
# have been one line and would have granted this instance every bucket in the
# account, including ones that do not exist yet.
#
# The instance can read and write the two prefixes it uses in one named bucket,
# and write to one named log group. Nothing else.
# ------------------------------------------------------------------------------
data "aws_iam_policy_document" "instance" {
  # Listing is a bucket-level action, so it cannot be scoped by object ARN. It is
  # scoped by prefix condition instead, so the instance cannot enumerate the rest
  # of the bucket.
  statement {
    sid       = "ListOwnPrefixes"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.logs.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = concat([for p in local.s3_prefixes : "${p}/*"], local.s3_prefixes)
    }
  }

  statement {
    sid = "ReadWriteOwnPrefixes"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = [for p in local.s3_prefixes : "${aws_s3_bucket.logs.arn}/${p}/*"]
  }

  # Log delivery for Docker's awslogs driver. The group itself is created by
  # Terraform, so the instance never needs CreateLogGroup - only to append.
  #
  # This policy grants what Stage 3 and Stage 4 actually use and nothing more.
  # Stage 5 adds the CloudWatch read permissions Grafana needs, at that point,
  # rather than granting them now against a future need - which is how policies
  # end up over-broad.
  statement {
    sid = "ShipLogs"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = [
      aws_cloudwatch_log_group.app.arn,
      "${aws_cloudwatch_log_group.app.arn}:*",
    ]
  }

}

resource "aws_iam_role_policy" "instance" {
  name_prefix = "${var.project_name}-instance-"
  role        = aws_iam_role.instance.id
  policy      = data.aws_iam_policy_document.instance.json
}

resource "aws_iam_instance_profile" "instance" {
  name_prefix = "${var.project_name}-"
  role        = aws_iam_role.instance.name
}

# ------------------------------------------------------------------------------
# Stage 5 additions.
#
# Kept as a separate policy rather than folded into the base one, so the split
# between "what the service needs to run" and "what the monitoring needs to read"
# stays visible - and so turning Grafana off actually removes the grant.
# ------------------------------------------------------------------------------
data "aws_iam_policy_document" "monitoring" {
  # The self-monitoring cron reads the service's own logs back out of CloudWatch
  # and feeds them through the detector.
  statement {
    sid = "ReadOwnLogs"
    actions = [
      "logs:FilterLogEvents",
      "logs:GetLogEvents",
    ]
    resources = [
      aws_cloudwatch_log_group.app.arn,
      "${aws_cloudwatch_log_group.app.arn}:*",
    ]
  }

  # PutMetricData has no resource-level permissions - the API does not support
  # them. The available control is a condition pinning the namespace, so a
  # compromised host cannot forge data into AWS/EC2 or any other namespace.
  statement {
    sid       = "PublishOwnMetricsOnly"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = [local.metric_namespace]
    }
  }

  # Grafana's CloudWatch datasource. Read-only, and all of these are list/get
  # APIs that AWS does not support resource-scoping on.
  dynamic "statement" {
    for_each = var.enable_grafana_read ? [1] : []
    content {
      sid = "GrafanaCloudWatchRead"
      actions = [
        "cloudwatch:GetMetricData",
        "cloudwatch:GetMetricStatistics",
        "cloudwatch:ListMetrics",
        "logs:DescribeLogGroups",
        "logs:StartQuery",
        "logs:StopQuery",
        "logs:GetQueryResults",
        "tag:GetResources",
      ]
      resources = ["*"]
    }
  }
}

resource "aws_iam_role_policy" "monitoring" {
  name_prefix = "${var.project_name}-monitoring-"
  role        = aws_iam_role.instance.id
  policy      = data.aws_iam_policy_document.monitoring.json
}
