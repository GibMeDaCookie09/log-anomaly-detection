data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-kernel-6.1-x86_64"]
  }
  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_key_pair" "admin" {
  count = var.ssh_public_key_path != "" ? 1 : 0

  key_name_prefix = "${var.project_name}-"
  public_key      = trimspace(file(pathexpand(var.ssh_public_key_path)))
}

resource "aws_instance" "app" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  subnet_id              = local.subnet_id
  vpc_security_group_ids = [aws_security_group.instance.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name
  key_name               = one(aws_key_pair.admin[*].key_name)

  associate_public_ip_address = true

  # IMDSv2 required, not merely available. With IMDSv1 still enabled, any
  # server-side request forgery in the application can read the instance
  # metadata endpoint and walk off with this instance's role credentials.
  # The hop limit of 1 stops a container from reaching it at all.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_size           = var.root_volume_gb
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    aws_region       = var.aws_region
    log_group        = aws_cloudwatch_log_group.app.name
    s3_bucket        = aws_s3_bucket.logs.bucket
    api_port         = var.api_port
    container_image  = var.container_image
    project_name     = var.project_name
    metric_namespace = local.metric_namespace
  })

  # user_data only runs on first boot. Without this, editing the bootstrap script
  # produces a clean plan and changes nothing on the running host - a silent
  # no-op is worse than an explicit replacement.
  user_data_replace_on_change = true

  lifecycle {
    # data.aws_ami is most_recent, so Amazon publishing a new AL2023 image would
    # otherwise propose destroying a healthy instance on an unrelated plan.
    # Pick up a new AMI deliberately:  terraform apply -replace=aws_instance.app
    ignore_changes = [ami]
  }

  tags = {
    Name = "${var.project_name}-app"
  }

  # Both policies, so the host never boots with a role that cannot yet ship
  # logs or publish metrics.
  depends_on = [aws_iam_role_policy.instance, aws_iam_role_policy.monitoring]
}

# Off by default. Free while attached to a *running* instance, billed hourly the
# moment it stops - and since the recommended workflow is destroy-when-idle, the
# address changes between sessions regardless. Worth turning on only if you
# intend to leave the instance up.
resource "aws_eip" "app" {
  count = var.allocate_elastic_ip ? 1 : 0

  instance = aws_instance.app.id
  domain   = "vpc"

  tags = {
    Name = "${var.project_name}-app"
  }
}
