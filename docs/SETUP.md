# Setup runbook

Getting this from a repository on disk to a running, self-monitoring service.

Work through it in order. Each phase leaves you with something that works, so you
can stop at the end of any of them. **Phases 1 and 2 cost nothing and need no AWS
account** — do them first, because they verify the parts that are currently only
verified locally.

| Phase | What you get | Time | Cost |
|---|---|---|---|
| 1 | CI green on GitHub | ~20 min | free |
| 2 | Published images on ghcr.io | ~10 min | free |
| 3 | Live AWS infrastructure | ~1–2 hrs | free tier |
| 4 | Automated deploys with rollback | ~20 min | free tier |
| 5 | Dashboards, and the demo that proves the loop | ~45 min | free tier |
| 6 | Teardown | ~2 min | — |

> **Optional but recommended:** install the GitHub CLI first. It makes repo
> creation, secrets and watching runs one command each instead of several web
> pages.
>
> ```
> winget install --id GitHub.cli -e --source winget
> ```
>
> Then `gh auth login`. Every step below gives both the `gh` command and the
> web-UI equivalent.

---

## Phase 1 — GitHub, and get CI green

This is the phase that validates what could not be checked locally: the Docker
build, the container smoke test, and the test matrix on Python 3.11 and 3.12.

### 1.1 Create the repository

```bash
gh repo create log-anomaly-detection --public --source=. --remote=origin
```

Or: create it at <https://github.com/new> named `log-anomaly-detection`, **without**
a README, .gitignore or licence — the repo already has them — then:

```bash
git remote add origin https://github.com/GibMeDaCookie09/log-anomaly-detection.git
```

> If you name it something other than `log-anomaly-detection`, update the badge
> URL at the top of `README.md` and `org.opencontainers.image.source` in the
> `Dockerfile`.

### 1.2 Push

```bash
git push -u origin main
```

### 1.3 Watch the first run

```bash
gh run watch
```

Or the **Actions** tab. Five jobs run: `test (3.11)`, `test (3.12)`, `lint`,
`deploy script`, `terraform`, `docker build`.

**Expect this first run to be slow** — every cache is cold. That is the point;
see 1.5.

**If something fails**, the likely candidates, in order:

- `docker build` — the only job never exercised locally. Read the smoke-test step
  output: it prints the `/health` response, or the container logs if it never
  became healthy.
- `terraform` — should pass, `validate` was run locally against the same
  provider version.
- `test` — if it fails on 3.11 but not 3.12, something used newer syntax.

### 1.4 Turn on branch protection

Settings → Branches → Add rule for `main`:

- Require status checks to pass → select **`CI passed`** only
- Require a pull request before merging

`CI passed` is the aggregate job. Selecting it rather than the individual jobs
means adding a Python version to the matrix later does not require editing these
settings.

### 1.5 Fill in the caching numbers

`README.md` has a table marked with a `TODO` for measured CI timings. Now you can
fill it in honestly:

1. Open the **first** run (cold — the caches did not exist).
2. Note the wall-clock duration of the `test (python 3.12)` and `docker build`
   jobs.
3. Push any trivial commit that does **not** touch `requirements*.txt`.
4. Note the same two durations on the warm run.
5. Put both numbers in the table and delete the `TODO` comment.

> Do not reuse the illustrative "3m10s → 47s" figure from anywhere. A measured
> before-and-after that you can explain is worth far more in an interview than an
> impressive one you cannot.

---

## Phase 2 — Publish an image

### 2.1 Tag a release

```bash
git tag v0.1.0 && git push origin v0.1.0
```

The **Release** workflow runs CI first, then builds and pushes to ghcr.io, then
pulls the image back and fails unless `/health` reports the commit it was built
from.

### 2.2 Check what landed

Your profile → **Packages** → `log-anomaly-detection`. You should see three tags:
`v0.1.0`, `0.1`, `sha-<7 chars>`, plus `latest`.

### 2.3 Decide public or private

The package is **private** by default. Both options work:

- **Leave it private** — the deploy workflow authenticates the instance with the
  run-scoped `GITHUB_TOKEN` and logs it out afterwards. Nothing extra to do.
- **Make it public** (Package settings → Change visibility) — simpler, and lets
  anyone reading your CV pull the image. For a portfolio project this is usually
  the better answer.

---

## Phase 3 — AWS

The only phase that can cost money. The order below puts the guardrails first.

### 3.1 Account, and which free tier you are on

Use an existing account or create one at <https://aws.amazon.com>.

**Check which free tier applies to you**, because AWS changed it: newer accounts
get a fixed credit grant that expires, rather than twelve months of monthly
allowances. Billing console → **Free tier**. The sizing here is conservative
under either model, but know which one you have before leaving anything running.

### 3.2 Billing alarm — before anything else

Terraform creates a `$1` budget, but that only exists *after* you apply. Set one
by hand now:

Billing → **Budgets** → Create budget → Cost budget → monthly, `$1`, alert at
100% actual and 80% forecast, to your email.

Two minutes, and it is the difference between noticing a mistake on day one and
noticing it on a statement.

### 3.3 Credentials

Do not use root credentials. IAM → Users → Create user → attach
**AdministratorAccess** → Security credentials → Create access key → *Command
Line Interface*.

> AdministratorAccess is broad, and deliberately so for a personal learning
> account — Terraform here creates IAM roles, S3 buckets, EC2 instances, log
> groups, alarms, SNS topics and budgets, and scoping a policy to exactly that
> set is a genuine piece of work in itself. Worth saying out loud in an
> interview: the *instance* role is tightly scoped, the *operator* credential is
> not, and those are different problems.

Install the CLI and configure it:

```bash
winget install --id Amazon.AWSCLI -e --source winget
```

```bash
aws configure
```

Verify — this should print your account ID:

```bash
aws sts get-caller-identity
```

### 3.4 SSH key

The deploy reaches the instance over SSH, so this is required, not optional.

```bash
ssh-keygen -t ed25519 -C "loganomaly-deploy" -f ~/.ssh/loganomaly_ed25519
```

Leave the passphrase empty — GitHub Actions cannot type one.

### 3.5 Variables

```bash
cd infra && cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars`:

| Variable | Value |
|---|---|
| `admin_cidr` | your address as a `/32` — `curl -s https://checkip.amazonaws.com` |
| `ssh_public_key_path` | `~/.ssh/loganomaly_ed25519.pub` |
| `budget_alert_email` | your email |
| `aws_region` | nearest region; `ap-south-1` is the default |
| `allocate_elastic_ip` | set `true` — see the note below |

> **Set `allocate_elastic_ip = true` while you are working through Phases 4 and
> 5.** Without it the public IP changes on every `terraform apply`, and you have
> to update the `DEPLOY_HOST` secret each time. It is free while the instance is
> *running*, and billed hourly the moment it stops — so if you stop the instance
> rather than destroying it, set it back to `false` first.
>
> Your `admin_cidr` is your home IP. If it changes, SSH and the API stop
> answering — re-run `apply` with the new value.

### 3.6 Apply

```bash
terraform init
```

```bash
terraform plan
```

Read the plan. Roughly 20 resources, no `destroy`, nothing unexpected.

```bash
terraform apply
```

### 3.7 Confirm the alarm subscription

AWS sends a confirmation email for the SNS topic. **Click the link.** Until you
do, the subscription is pending and no alarm will ever reach you — Terraform
cannot do this for you and shows it as pending indefinitely.

### 3.8 Check what you got

```bash
terraform output
```

```bash
ssh -i ~/.ssh/loganomaly_ed25519 ec2-user@$(terraform output -raw public_ip) "docker --version && cat /etc/loganomaly/env"
```

The instance boots with Docker installed and **nothing running** — that is
correct. `container_image` is empty by default because the first real image comes
from a deploy, not from Terraform.

> If `terraform plan` fails on `data.aws_vpc.default`, your account has no
> default VPC. `aws ec2 create-default-vpc` restores it.

---

## Phase 4 — Wire up deploys

### 4.1 Secrets

```bash
gh secret set DEPLOY_HOST --body "$(terraform -chdir=infra output -raw public_ip)"
```

```bash
gh secret set DEPLOY_SSH_KEY < ~/.ssh/loganomaly_ed25519
```

Or: Settings → Secrets and variables → Actions → New repository secret, twice.
`DEPLOY_SSH_KEY` is the **private** key — the whole file including the
`-----BEGIN` and `-----END` lines.

### 4.2 Deploy

Push anything to `main`, or trigger it by hand:

```bash
gh workflow run deploy.yml
```

The job runs CI, builds, pushes, copies the deploy scripts over, rolls the
container, health-gates it, verifies the served SHA, installs the monitoring
timer, and checks the public URL from outside the instance.

### 4.3 Verify

```bash
curl -s "http://$(terraform -chdir=infra output -raw public_ip):8000/health"
```

`build.sha` should match the commit you deployed. If the API is unreachable but
the workflow succeeded, `api_ingress_cidrs` is restricted to your `admin_cidr` —
which is the safe default. To open it for a demo, set
`api_ingress_cidrs = ["0.0.0.0/0"]` and re-apply, knowing the API has no
authentication.

### 4.4 Confirm the timer is running

```bash
ssh -i ~/.ssh/loganomaly_ed25519 ec2-user@$(terraform -chdir=infra output -raw public_ip) "systemctl list-timers self-monitor.timer"
```

---

## Phase 5 — Dashboards, and the demo

### 5.1 Grafana

```bash
scp -i ~/.ssh/loganomaly_ed25519 -r monitoring ec2-user@$(terraform -chdir=infra output -raw public_ip):~/
```

Then on the instance, create `monitoring/.env` from `.env.example` with a real
password, and:

```bash
cd monitoring && docker compose up -d
```

Port 3000 is **not** open in the security group, deliberately. Tunnel to it:

```bash
ssh -i ~/.ssh/loganomaly_ed25519 -N -L 3000:localhost:3000 ec2-user@$(terraform -chdir=infra output -raw public_ip)
```

Open <http://localhost:3000>. The dashboard is already provisioned.

> Metrics only exist once traffic has flowed. Give it 10–15 minutes, or generate
> some: `for i in $(seq 1 50); do curl -s .../health > /dev/null; done`

### 5.2 Break it on purpose

This is the strongest artifact in the project. Read the full procedure in the
main `README.md` under **Breaking it on purpose**, then capture, in one sitting:

1. The **deploy workflow failing with the rollback firing** — summary reads
   *"Deploy failed — rolled back"*, and the service is still up on the previous
   image.
2. `curl /health` returning **503** on the broken container.
3. The **`api-5xx-rate` alarm in ALARM** state in CloudWatch.
4. The **self-monitoring run flagging the burst** — force it rather than waiting:
   `sudo systemctl start self-monitor.service && journalctl -u self-monitor -n 50`.
   Look for `"signals": ["rarity"]` and templates containing
   `request GET /health server_error`.
5. The **Grafana dashboard** showing the 5xx spike and `AnomaliesDetected`
   rising together.

Put those five screenshots in `docs/` and link them from the README. That
sequence — a deployment failure detected by the thing being deployed — is the
whole point of the project, and it is much more convincing shown than described.

---

## Phase 6 — Tear down when you are not working on it

```bash
cd infra && terraform destroy
```

This deletes the bucket and its contents (`force_destroy = true`), the instance,
the log group and the alarms. Re-running `apply` rebuilds everything in a few
minutes; only the public IP changes.

The GitHub side — repository, images on ghcr.io, workflow history — costs nothing
and stays. Screenshots and the README are what a reviewer actually looks at, so
there is no reason to leave AWS running between sessions.

**Check the billing console once a week regardless.** The `$1` budget is a
backstop, not a guarantee.
