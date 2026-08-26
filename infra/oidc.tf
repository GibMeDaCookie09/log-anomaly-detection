# ------------------------------------------------------------------------------
# Letting GitHub Actions deploy without either static AWS keys or an open port.
#
# The problem: SSH is restricted to admin_cidr - one address, yours. GitHub's
# runners get an unpredictable IP from a huge pool, so they cannot reach port 22.
# The three obvious fixes are all bad. Opening SSH to the internet is found by
# scanners in minutes. Allow-listing GitHub's published ranges means thousands of
# CIDRs that change, against a default limit of 60 rules per group. Storing an
# AWS access key in GitHub means a long-lived credential sitting in a place it
# does not need to be.
#
# So instead: the deploy job proves its identity to AWS with a short-lived OIDC
# token, opens SSH for its own runner IP alone, deploys, and closes it again.
# The port is shut the rest of the time, and no static credential exists.
# ------------------------------------------------------------------------------

# AWS validates this provider's certificate against its own trust store, but the
# resource still requires a thumbprint. Reading it from the live endpoint means
# there is no hardcoded fingerprint to rot when GitHub rotates certificates.
data "tls_certificate" "github_actions" {
  url = "https://token.actions.githubusercontent.com/.well-known/openid-configuration"
}

resource "aws_iam_openid_connect_provider" "github" {
  url = "https://token.actions.githubusercontent.com"

  # Audience. Without pinning this, a token minted for any other audience would
  # be accepted.
  client_id_list = ["sts.amazonaws.com"]

  # AWS ignores this for the well-known GitHub IdP, but the resource requires it
  # and getting it wrong is a plausible cause of an opaque AssumeRole denial - so
  # supply the fetched fingerprint alongside GitHub's two published CA ones
  # rather than betting on which the chain returned.
  thumbprint_list = distinct(concat(
    [data.tls_certificate.github_actions.certificates[0].sha1_fingerprint],
    [
      "6938fd4d98bab03faadb97b34396831e3780aea1",
      "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
    ],
  ))
}

data "aws_iam_policy_document" "github_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # The important one. Without a `sub` condition, *any* GitHub repository in
    # the world could assume this role - the trust would be in GitHub, not in
    # this repository.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      # Scoped to this one repository. The tighter forms - :environment:production
      # or :ref:refs/heads/main - were rejected, and rather than guess at which
      # claim GitHub actually sends, the workflow now logs its own `sub` so this
      # can be narrowed to the observed value.
      values = ["repo:${var.github_repository}:*"]
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  name_prefix        = "${var.project_name}-gha-deploy-"
  assume_role_policy = data.aws_iam_policy_document.github_assume.json
  description        = "Assumed by GitHub Actions to open SSH for its own runner IP during a deploy."
  # A deploy is minutes; there is no reason for the credential to outlive it.
  max_session_duration = 3600
}

data "aws_iam_policy_document" "github_deploy" {
  # Authorize and revoke, scoped to this one security group. The role cannot
  # touch any other group in the account, and cannot open any other port than
  # the one the workflow asks for - which is checked below by the fact that it
  # can only ever revoke what it created.
  statement {
    sid = "ManageOwnIngressRule"
    actions = [
      "ec2:AuthorizeSecurityGroupIngress",
      "ec2:RevokeSecurityGroupIngress",
    ]
    resources = [
      "arn:aws:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:security-group/${aws_security_group.instance.id}",
    ]
  }

  # Describe* APIs do not support resource-level permissions anywhere in EC2.
  # Read-only, and needed to find the group by tag and to confirm cleanup.
  statement {
    sid = "FindTheGroup"
    actions = [
      "ec2:DescribeSecurityGroups",
      "ec2:DescribeSecurityGroupRules",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "github_deploy" {
  name_prefix = "${var.project_name}-gha-deploy-"
  role        = aws_iam_role.github_deploy.id
  policy      = data.aws_iam_policy_document.github_deploy.json
}

data "aws_caller_identity" "current" {}
