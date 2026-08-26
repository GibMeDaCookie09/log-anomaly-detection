# The default VPC, deliberately. A custom VPC is not hard, but a private subnet
# needs a NAT gateway to reach ghcr.io, and a NAT gateway is roughly $32/month -
# more than the rest of this project combined, and not free tier. A single public
# subnet with a tight security group is the correct shape at this size.
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

locals {
  # Sorted so the chosen subnet is stable across plans; an unsorted data source
  # can reorder and quietly propose replacing the instance.
  subnet_id = sort(data.aws_subnets.default.ids)[0]

  api_cidrs = length(var.api_ingress_cidrs) > 0 ? var.api_ingress_cidrs : [var.admin_cidr]
}

resource "aws_security_group" "instance" {
  name_prefix = "${var.project_name}-instance-"
  description = "Least-privilege ingress for the ${var.project_name} API host."
  vpc_id      = data.aws_vpc.default.id

  tags = {
    Name = "${var.project_name}-instance"
  }

  # The security group is referenced by the instance, so it cannot be destroyed
  # before its replacement exists.
  lifecycle {
    create_before_destroy = true
  }
}

# Two ports open, both explicitly. Nothing else: no 80, no 443, no ICMP.
resource "aws_vpc_security_group_ingress_rule" "ssh" {
  security_group_id = aws_security_group.instance.id
  # No apostrophe: AWS restricts security group rule descriptions to
  # a-zA-Z0-9 and ._-:/()#,@[]+=&;{}!$* and rejects anything else.
  description = "SSH from admin_cidr only"
  cidr_ipv4   = var.admin_cidr
  from_port   = 22
  to_port     = 22
  ip_protocol = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "api" {
  for_each = toset(local.api_cidrs)

  security_group_id = aws_security_group.instance.id
  description       = "Anomaly detection API"
  cidr_ipv4         = each.value
  from_port         = var.api_port
  to_port           = var.api_port
  ip_protocol       = "tcp"
}

# Egress stays open: the host has to reach ghcr.io to pull images, and the S3 and
# CloudWatch endpoints to ship logs. Restricting this properly means VPC
# endpoints, which are not free.
resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.instance.id
  description       = "Outbound to registry, S3 and CloudWatch"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
