# Infrastructure

Terraform for the AWS side of the platform: one instance running the container,
one bucket for log ingestion and results, one log group, and an IAM role scoped
to exactly those two things.

```
                    ┌─────────────────────────────┐
   ghcr.io ────────▶│  EC2 t3.micro (AL2023)      │
   (image pull)     │  docker run loganomaly      │
                    │  IMDSv2 required            │
                    └──────┬───────────────┬──────┘
                           │               │
                  instance role      awslogs driver
                           │               │
                           ▼               ▼
                    ┌────────────┐  ┌──────────────────┐
                    │ S3 bucket  │  │ CloudWatch Logs  │
                    │ logs/      │  │ /loganomaly/app  │
                    │ results/   │  └──────────────────┘
                    └────────────┘
```

## Prerequisites

```bash
aws configure                 # credentials with permission to create the above
ssh-keygen -t ed25519 -C "loganomaly-deploy" -f ~/.ssh/loganomaly_ed25519
curl -s https://checkip.amazonaws.com     # your address, for admin_cidr
```

> **This assumes the account has a default VPC.** Most do, but accounts created
> under some organisation policies have had theirs deleted. `terraform plan` will
> fail on `data.aws_vpc.default` if so; recreate it with
> `aws ec2 create-default-vpc`, or point `network.tf` at an existing VPC and
> public subnet.

## Usage

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars   # then edit it
terraform init
terraform plan
terraform apply
```

`terraform output` gives the API URL, the health URL and a ready-to-paste SSH
command. When you are not actively working:

```bash
terraform destroy
```

## Least privilege

The instance role grants three things and nothing else:

| Action | Scoped to |
|---|---|
| `s3:ListBucket` | this bucket, further conditioned on the `logs/` and `results/` prefixes |
| `s3:GetObject` `PutObject` `DeleteObject` | `<bucket>/logs/*` and `<bucket>/results/*` only |
| `logs:CreateLogStream` `PutLogEvents` `DescribeLogStreams` | this log group and its streams |

What that deliberately excludes:

**No AWS managed policies.** `AmazonS3FullAccess` would have been one line and
would have granted this instance every bucket in the account — including buckets
that do not exist yet. Managed policies are convenient precisely because they are
broad.

**No resource wildcards.** Every statement names an ARN. The one action that
genuinely cannot be resource-scoped is not granted at all: Docker's `awslogs`
driver needs only log delivery, so the CloudWatch *agent* is not installed and
its `cloudwatch:PutMetricData` permission is not granted. Instance CPU already
arrives as a default EC2 metric without it.

**No `CreateLogGroup`.** Terraform creates the group, so the instance only ever
needs to append to it. An application that can create log groups can also create
a thousand of them.

**Prefix-scoped listing.** `s3:ListBucket` is a bucket-level action and cannot be
restricted by object ARN, so it is restricted by an `s3:prefix` condition
instead. Without that, "scoped to two prefixes" would still let the instance
enumerate the entire bucket.

The permissions Grafana needs to read CloudWatch are added in Stage 5, when
something actually uses them — rather than now, against a future need. Granting
ahead of use is how policies drift broad.

## Other security choices

**IMDSv2 is required, not merely enabled.** With IMDSv1 still accepted, any
server-side request forgery in the application can read the metadata endpoint and
walk off with the instance role's credentials — the mechanism behind the 2019
Capital One breach. `http_put_response_hop_limit = 1` additionally stops a
container from reaching it.

**SSH is restricted to one address and validated as such.** `admin_cidr` has a
validation rule that rejects `0.0.0.0/0` outright. An open SSH port is found by
scanners within minutes.

**The API port defaults to the same single address.** Opening it publicly is a
one-line change, but it is a decision you have to make rather than a default you
inherit — the service has no authentication.

**Root volume encrypted, bucket encrypted, all public access blocked, ACLs
disabled** so access is governed by the IAM policy and nothing else.

## Cost

Everything here is chosen to sit inside the AWS Free Tier:

| Resource | Free tier allowance |
|---|---|
| EC2 t3.micro | 750 hrs/month |
| EBS gp3, 20 GB | 30 GB/month |
| Public IPv4 | 750 hrs/month |
| S3 | 5 GB storage, 20k GET, 2k PUT |
| CloudWatch Logs | 5 GB ingestion |
| AWS Budgets | first 2 budgets free |

> **Check which free tier your account is on.** AWS moved new accounts to a
> credit-based model (a fixed credit grant, expiring after a set period) rather
> than the older 12-months-of-monthly-allowances model. Which one applies depends
> on when the account was opened. The sizing above is conservative under either,
> but do not assume the 12-month allowances without confirming.

Three guardrails, because "it should be free" is a claim about the future:

- **A budget alert at $1**, with notifications at 1% actual (one cent — expected
  spend is zero, so any spend is the signal) and 100% forecasted.
- **Lifecycle expiry on the bucket** at 30 days, plus abort of incomplete
  multipart uploads, which otherwise bill invisibly.
- **Retention on the log group.** The default is "never expire", which is how a
  free-tier log group quietly starts billing months later.

The two things most likely to cost money if you change them: a NAT gateway
(~$32/month, which is why this uses the default VPC's public subnet), and an
Elastic IP left allocated while the instance is stopped. `allocate_elastic_ip`
is off by default for that reason.

## State

State is local. A remote S3 backend needs a bucket, and the only configuration
that knows how to create one is this one — so bootstrapping it from here is
circular. For a single operator that trade-off favours local state; `versions.tf`
carries a commented backend block for when it does not.

`terraform.tfstate` is gitignored. It contains every resource ID and can contain
secrets.
