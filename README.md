# Log Anomaly Detection with LLM Explanations

[![CI](https://github.com/GibMeDaCookie09/log-anomaly-detection/actions/workflows/ci.yml/badge.svg)](https://github.com/GibMeDaCookie09/log-anomaly-detection/actions/workflows/ci.yml)

Unsupervised anomaly detection over system logs, with an LLM layer that turns a
score into something an on-call engineer can act on.

```
raw logs → Drain3 template parsing → window aggregation → anomaly scoring → LLM explanation
```

Benchmarked on **LogHub BGL** (Blue Gene/L supervisor logs), which ships
per-line fault labels, so every number below is measured against ground truth
rather than asserted.

---

## Results

BGL sample: 2,000 lines, 105 distinct templates, 143 labelled anomalies (7.1%).
`k` = true anomaly count, so every detector gets the same alert budget.

### Line-level detection

| Detector | Features | ROC AUC | Avg Precision | F1@k |
|---|---|---|---|---|
| template-rarity | none (control) | 0.805 | 0.181 | 0.112 |
| kNN distance | TF-IDF + SVD | 0.716 | 0.203 | 0.133 |
| Isolation Forest | TF-IDF + SVD | 0.803 | 0.184 | 0.133 |

### Window-level detection (template count vectors)

| Window | Detector | ROC AUC | Avg Precision | F1@k |
|---|---|---|---|---|
| 10 | kNN distance | 0.793 | 0.399 | 0.475 |
| 10 | Isolation Forest | 0.823 | 0.472 | 0.450 |
| 20 | kNN distance | 0.821 | 0.570 | 0.643 |
| 20 | Isolation Forest | 0.816 | 0.550 | 0.571 |
| 50 | kNN distance | 0.752 | 0.648 | 0.588 |
| **50** | **Isolation Forest** | **0.824** | **0.699** | **0.765** |

Reproduce:

```bash
PYTHONPATH=src python scripts/benchmark.py          # line-level
PYTHONPATH=src python scripts/benchmark_windows.py  # window-level
```

---

## What the numbers actually say

**1. Window aggregation is the whole ballgame — F1 0.133 → 0.765, a 5.8x
improvement.** Everything else is a rounding error next to it.

The reason is worth stating precisely: a line like `kernel: page allocation
failure` is *ordinary in isolation*. What makes it anomalous is its context —
what preceded it, and how often it fired in a short span. Line-level scoring
throws that context away by construction, so no amount of embedding
sophistication rescues it.

**2. At line level, the no-embedding control beat TF-IDF embeddings on ROC AUC
(0.805 vs 0.716–0.803).** A pure inverse-log-frequency heuristic with no vector
representation at all matched or beat the embedding pipeline. On this dataset,
at that granularity, the embeddings did not earn their complexity — and the
`TemplateRarityDetector` exists specifically so that claim is testable rather
than assumed.

**3. Average precision rises monotonically with window size (0.47 → 0.70) but
window count falls (200 → 40).** That's a real operational trade-off, not a
free win: larger windows localise the fault less precisely, so an analyst has
more lines to read per alert.

---

## Design decisions

**The LLM does not detect anything.** Detection is statistical, deterministic and
reproducible. The LLM only *explains* an already-flagged window. Putting an LLM
in the detection path would make results non-reproducible, unauditable, and
expensive per log line.

**Labels are used for evaluation only, never for fitting.** In production there
are no labels. Fitting on them would make this a supervised classifier, which is
a different and much easier problem.

**Prompts carry contrast.** Each prompt includes templates from a typical *normal*
window alongside the flagged one. Without the contrast the model paraphrases the
logs instead of reasoning about them.

**Explanations cache by content hash.** Templates repeat constantly; you should
never pay twice for the same explanation.

**A stub LLM backend ships in-tree.** The full pipeline and the entire test suite
run with no API key and no network. A test suite that needs a credential is a
test suite that stops getting run.

**Backends are pluggable behind ABCs** (`Embedder`, `Detector`, `LlmClient`).
Swapping TF-IDF for neural embeddings, or the stub for a real model, touches one
constructor argument and nothing else.

---

## Layout

```
src/loganomaly/
  parse.py      Drain3 template extraction
  window.py     sliding windows + normalised template count vectors
  embed.py      TF-IDF / sentence-transformers / hosted API
  detect.py     kNN distance, Isolation Forest, frequency control
  evaluate.py   ROC AUC, average precision, precision/recall @ k
  explain.py    prompt construction, LLM clients, response cache
  pipeline.py   fit-once / score-many orchestration
  ingest.py     canonicalises structured records - see Stage 5
  observability.py  structured access log (the metric-filter contract)
  api.py        FastAPI service (app factory, /health deploy gate)
scripts/
  benchmark.py          line-level comparison
  benchmark_windows.py  window-size sweep
  self_monitor.py       the loop: read own logs, fit, score, publish
tests/                  34 tests, fully offline

infra/                  Terraform: EC2, S3, IAM, security group, CloudWatch
  iam.tf                prefix-scoped instance role - see infra/README.md
  ec2.tf                t3.micro, IMDSv2 required, encrypted root volume
  user_data.sh.tftpl    first-boot bootstrap (Docker only, no deploy logic)
deploy/
  deploy.sh             roll, health-gate, roll back. Runs on the host.
  test_deploy.sh        7 rollback scenarios, stubbed docker/curl
  run_self_monitor.sh   feeds the service's own logs back through it
  self-monitor.timer    every 15 minutes
monitoring/             Grafana over CloudWatch, provisioned from files
.github/workflows/
  ci.yml                tests, lint, shellcheck, image build + smoke test
  release.yml           tag -> ghcr.io, with provenance verification
  deploy.yml            merge to main -> build, push, roll, verify
```

---

## Running it

```bash
pip install -r requirements.txt
pytest                                          # 34 passed
PYTHONPATH=src python scripts/benchmark_windows.py
PYTHONPATH=src uvicorn loganomaly.api:app --reload
```

`GET /health` → pipeline state · `POST /score` → top-k alerts with explanations
· `/docs` → OpenAPI UI

```bash
docker build -t loganomaly . && docker run -p 8000:8000 loganomaly
```

---

---

## Delivery pipeline

> Standing this up from scratch — GitHub, AWS, first deploy, the demo — is
> a step-by-step runbook in [`docs/SETUP.md`](docs/SETUP.md).

The service that detects anomalies in logs is deployed by a pipeline that emits
logs, which the service then ingests and monitors. A misbehaving deploy shows up
in the tool's own alert stream.

### Stage 1 — CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml))

Runs on every push and every pull request.

| Job | What it does |
|---|---|
| `test` | `pytest` on Python 3.11 and 3.12 (matrix, `fail-fast: false` so both versions report) |
| `lint` | `ruff check` and `ruff format --check` |
| `deploy script` | `shellcheck` over `deploy/*.sh`, then the seven rollback scenarios |
| `terraform` | `fmt -check` and `validate` against the real provider schema |
| `docker build` | builds the image, starts it, and polls `/health` until it answers |
| `CI passed` | aggregate job — the single required status check for branch protection |

Three decisions in there are worth defending:

**The Docker job runs the container, it does not only build it.** A successful
build proves the image compiles; it says nothing about whether the process comes
up. The smoke test hits the same `/health` endpoint that gates deploys in Stage 4,
so CI and production agree on what "working" means.

**`/health` returns 503 when the pipeline is unfitted, not 200.** It previously
returned `{"status": "ok"}` with `fitted: false` — which meant a container that
answered every `/score` with a 503 would still sail through a post-deploy health
check. A gate that cannot fail is not a gate. `/health` also reports the build
version and commit SHA, so a deploy (or a rollback) can be confirmed as having
actually taken effect rather than assumed.

**One aggregate check, not four.** Branch protection points at `CI passed`, so
adding a Python version to the matrix does not require touching repo settings.

### Dependency caching

`actions/setup-python` restores `~/.cache/pip`, keyed on a hash of
`requirements.txt` and `requirements-dev.txt`. `scikit-learn`, `scipy` and
`pandas` are large wheels and downloading them on every run dominates the job.
The Docker job caches build layers separately via `type=gha`, so the
`pip install` layer only rebuilds when the requirements files change.

<!-- TODO: fill in from real runs before submitting. Method: open the first CI
     run (cold cache, the caches did not exist yet) and a later run on an
     unchanged requirements.txt (warm), and read the wall-clock duration of each.
     Do not quote numbers you have not measured. -->

| | Cold cache | Warm cache |
|---|---|---|
| `test` job (3.12) | _measure_ | _measure_ |
| `docker build` job | _measure_ | _measure_ |

### Stage 2 — Image registry ([`.github/workflows/release.yml`](.github/workflows/release.yml))

Pushing a `v*` tag builds and publishes to GitHub Container Registry:

```
ghcr.io/<owner>/log-anomaly-detection:v0.1.0      # what a human asks for
ghcr.io/<owner>/log-anomaly-detection:sha-a1b2c3d # what a deploy records
ghcr.io/<owner>/log-anomaly-detection:latest
```

```bash
git tag v0.1.0 && git push origin v0.1.0
```

**Every image carries two tags.** Version tags are for people and can be moved or
deleted; the SHA tag is immutable and is what the deploy records, so a container
running in six months still traces back to one commit.

**The tag runs CI first.** A tag push does not match `ci.yml`'s branch filter, so
without a gate you can tag a broken commit and publish it. `release.yml` calls
`ci.yml` as a reusable workflow and only publishes if it passes.

**Provenance is verified, not asserted.** After pushing, the workflow pulls the
image back by its SHA tag, starts it, and fails the release unless `/health`
reports the commit and version it was built from. `BUILD_SHA` and `BUILD_VERSION`
are `ARG`s declared after the `COPY` layers, so a new commit invalidates only the
cheap tail of the build rather than the dependency layer.

Authentication uses the automatic `GITHUB_TOKEN` with `packages: write` — no PAT
to create, rotate or leak.

### Stage 3 — Infrastructure as code ([`infra/`](infra/))

Terraform for one EC2 t3.micro running the container, one S3 bucket for log
ingestion and results, one CloudWatch log group, a security group open on exactly
two ports, and an IAM role scoped to precisely those resources.

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars   # your IP, your SSH key, your email
terraform init && terraform apply
```

[`infra/README.md`](infra/README.md) covers the security and cost reasoning in
full. The parts worth stating here:

**Least privilege is specific, not a label.** The instance role names ARNs in
every statement. No AWS managed policy — `AmazonS3FullAccess` is one line and
grants every bucket in the account, including ones that do not exist yet. No
`CreateLogGroup`, because Terraform creates the group and the instance only ever
appends. `s3:ListBucket` cannot be scoped by object ARN, so it carries an
`s3:prefix` condition instead; without it, "scoped to two prefixes" would still
allow enumerating the whole bucket.

**Permissions are granted when something uses them.** Docker's `awslogs` driver
handles log delivery, so the CloudWatch agent is not installed and its
unscopable `cloudwatch:PutMetricData` is not granted. Instance CPU arrives as a
default EC2 metric anyway. Grafana's read permissions land in Stage 5, not now.

**IMDSv2 required, not merely enabled.** With v1 still accepted, an SSRF in the
application reads the metadata endpoint and leaves with the instance's
credentials — the Capital One mechanism. The hop limit of 1 stops a container
reaching it at all.

**The free tier is not assumed, it is enforced.** A $1 budget alert that fires at
one cent actual and 100% forecasted, lifecycle expiry on the bucket, retention on
the log group (the default is *never expire*), and no NAT gateway — which is why
this uses the default VPC's public subnet rather than a private one. The
`root_volume_gb` variable has a validation rule capping it at 30 GB.

**No Elastic IP by default.** Free while attached to a running instance, billed
the moment it stops. Since the recommended workflow is `terraform destroy` when
idle, the address changes between sessions either way.

### Stage 4 — Continuous deployment ([`deploy.yml`](.github/workflows/deploy.yml), [`deploy/deploy.sh`](deploy/deploy.sh))

Merging to `main` runs CI, builds and pushes the image, then SSHes to the
instance and rolls the container behind a health gate.

The interesting part is not the happy path. It is what happens when the deploy is
bad:

```
pull new image          ← before stopping anything: a registry failure
                          must not cause an outage
  ↓
start new container
  ↓
poll /health for 30s ────────── fails ──→ restore previous image
  ↓ passes                                  ↓
verify build.sha matches ─── wrong ──→   poll /health for 90s
  ↓ matches                                 ↓ passes      ↓ fails
record last-known-good                    exit 1        exit 2
exit 0                                  (service up)  (SERVICE DOWN)
```

**Exit codes are distinct because the responses are different.** `1` means the
change did not ship but the service is fine — annoying, look at it tomorrow. `2`
means nobody is serving traffic — look at it now. Collapsing both into "failed"
throws away the only thing you actually need to know at 2am.

**Health alone is not sufficient, so the SHA is checked too.** If the new
container had silently failed to replace the old one, `/health` would answer
happily — from the *previous* version — and the deploy would report success
having shipped nothing. `/health` reports `build.sha`, and the deploy fails
unless it matches the commit being deployed.

**The rollback path is tested.** It is the least-exercised and most important
branch in the project: in normal operation it never runs, so the first time you
find out whether it works would otherwise be during an outage.
[`deploy/test_deploy.sh`](deploy/test_deploy.sh) stubs out `docker` and `curl`
and asserts all seven outcomes — healthy, unhealthy-with-rollback,
rollback-also-failed, pull-failed, wrong-SHA, first-deploy, and
first-deploy-broken-with-no-rollback-target. It runs in CI on every PR.

**Deploys do not overlap.** `cancel-in-progress` is deliberately `false`:
cancelling a deploy halfway through is an excellent way to end up with nothing
running.

**Registry credentials on the host are ephemeral.** GHCR packages are private by
default, so the instance must authenticate to pull. The workflow passes the
run-scoped `GITHUB_TOKEN` over SSH and logs the host out again in an `always()`
step, so no long-lived PAT is left in the instance's docker config.

**One check comes from outside the host.** `deploy.sh` polls `localhost`, which
cannot tell you whether the security group, the port mapping and the public
address work. The workflow curls the public URL separately.

`workflow_dispatch` takes an `image_tag` input, so rolling forward or back to any
published tag is a button rather than an SSH session.

#### Required repository configuration

| Kind | Name | Value |
|---|---|---|
| Secret | `DEPLOY_SSH_KEY` | private half of the key in `ssh_public_key_path` |
| Secret | `DEPLOY_HOST` | `terraform output -raw public_ip` |
| Variable | `DEPLOY_USER` | optional, defaults to `ec2-user` |
| Variable | `API_PORT` | optional, defaults to `8000` |

`DEPLOY_HOST` changes on every `terraform apply` unless you set
`allocate_elastic_ip = true`. If you are actively iterating on CD, set it — the
EIP is free while the instance is running.

### Stage 5 — Observability, and the loop ([`monitoring/`](monitoring/), [`scripts/self_monitor.py`](scripts/self_monitor.py))

```
    request  ──▶  structured JSON access log  ──▶  stdout
                                                     │  docker awslogs driver
                                                     ▼
                                            CloudWatch Logs
                                             │              │
                              metric filters │              │ self_monitor.py
                                             ▼              │ reads it back
                        RequestCount / ServerErrors         │ every 15 min
                        / LatencyMs                         ▼
                                             │        fit on baseline,
                                             │        score recent
                                             ▼              │
                                     alarms ──▶ SNS ◀───────┤
                                             │              │
                                             ▼              ▼
                                        Grafana      AnomaliesDetected
                                                     + results/ in S3
```

The service that detects anomalies in logs is monitored by feeding it the logs
its own infrastructure produces. A failed deploy shows up in its own alert
stream.

#### From logs to metrics, without an agent

The app emits one JSON line per request (`method`, `path`, `status`,
`duration_ms`). Docker's `awslogs` driver forwards stdout to CloudWatch, and
CloudWatch metric filters turn those lines into `RequestCount`, `ServerErrors`
and `LatencyMs`. No agent to install, no log file to rotate, and no
`PutMetricData` permission on the request path.

The log format is a contract, so it is tested
([`tests/test_api.py`](tests/test_api.py)) — a metric filter that stops matching
produces no metric, which looks exactly like a service receiving no traffic.

Two details that decide whether the numbers mean anything: `RequestCount` and
`ServerErrors` set `default_value = 0` so idle periods read as zero rather than a
gap, and `LatencyMs` deliberately does **not** — filling idle minutes with 0 ms
would drag every latency average toward zero and hide real regressions.

#### Alarms

| Alarm | Fires on |
|---|---|
| `api-5xx-rate` | `100 * (errors / IF(requests > 0, requests, 1))` above 5% for 2 periods |
| `instance-cpu` | CPU above 80% for 2 periods — a t3.micro pegs briefly on every deploy while the pipeline fits, so one period would be pure noise |
| `instance-status` | EC2 status check failing — the host itself is sick |
| `anomalies-detected` | the detector flags more than 3 windows of the service's own logs in 15 minutes |

All route to one SNS topic. `treat_missing_data = "notBreaching"` on the rate
alarms, because no traffic is not an outage — and an alarm that goes
`INSUFFICIENT_DATA` every quiet night trains you to ignore it.

#### Two things that had to be fixed before the loop detected anything

Pointing the detector at the service's own logs did not work on the first
attempt, and both reasons are worth stating.

**1. Drain3 masks the status code.** The template miner replaces tokens that vary
between otherwise-identical lines — correct for unstructured logs, destructive
for structured ones. Fed raw JSON access logs, it produced:

```
{"ts": <*> "schema": 1, "event": "request", "method": <*> "path": <*> "status": <*> ...
```

A 200 and a 503 collapse into one template. On a 960-line sample containing a
40-request burst of 503s: 6 templates, **zero detections**.

[`ingest.py`](src/loganomaly/ingest.py) canonicalises structured records into
event strings whose discriminative fields are *words* — `request GET /health
server_error`. Words survive masking; numbers do not. The similarity threshold
also goes up from 0.4 to 0.7, because a canonicalised event is four tokens and
one differing token is a 25% difference that 0.4 absorbs.

**2. Isolation Forest is blind to new templates — the one signal that matters
most.** sklearn splits a node on a random feature between that feature's min and
max *within the training data*. A template that never appeared in the baseline is
a dimension of zero variance, so no tree can ever split on it. A bad deploy's
defining characteristic is log lines nobody has seen before, and the forest
cannot see them.

Measured on the same sample: the 503 burst scored **0.606** against a baseline
maximum of **0.706** — *less* anomalous than ordinary traffic jitter, and ranked
below two false positives.

The fix reuses the frequency control the library already ships. Inverse
log-frequency scores an unseen template highly by construction. A window is
flagged if either signal fires, and `signals` records which:

| | before | after |
|---|---|---|
| burst detected | no | yes — rarity 6.58 vs 0.69 threshold |
| burst rank | not flagged | 1st, 2nd, 3rd |

This is the same lesson the line-level benchmark taught: the simple frequency
heuristic earns its place, and the sophisticated detector does not automatically
win. Both are in [`tests/test_self_monitor.py`](tests/test_self_monitor.py) as
regression tests, including the raw-JSON collapse.

#### The self-monitoring run

A systemd timer runs every 15 minutes. It pulls the last two hours from
CloudWatch, fits on the older three quarters, scores the most recent quarter
against it, archives the raw batch and the verdict to S3, and publishes
`AnomaliesDetected`.

The threshold comes from the baseline's own score distribution (95th percentile)
rather than a constant, so it adapts to how noisy the service normally is. If the
baseline is perfectly uniform — nothing for Isolation Forest to split on — the
run reports `degenerate_baseline` rather than a confident zero.

It runs the **currently deployed image**, read from `last_good_image`, so the
detector doing the monitoring is the build being monitored.

One consequence of the IMDSv2 hop limit of 1: a bridged container cannot reach
the metadata endpoint, which is the point — an SSRF in the API cannot steal the
instance's credentials. So the host resolves credentials with
`aws configure export-credentials` and passes them in as short-lived environment
variables.

#### Grafana

```bash
scp -r monitoring/ ec2-user@<host>:~/
ssh ec2-user@<host> 'cd monitoring && cp .env.example .env && vi .env'
ssh ec2-user@<host> 'cd monitoring && docker compose up -d'

# port 3000 is NOT in the security group - reach it over a tunnel
ssh -N -L 3000:localhost:3000 ec2-user@<host>
```

Then <http://localhost:3000>. The dashboard is provisioned from
[`monitoring/dashboards/loganomaly.json`](monitoring/dashboards/loganomaly.json)
and is read-only in the UI — the file in the repo is the source of truth, so it
cannot be edited into a state nobody can reproduce.

`network_mode: host` is load-bearing rather than lazy: Grafana authenticates with
the instance role via IMDS, and at hop limit 1 a bridged container is one hop too
far. Host networking puts it in the host's namespace so it reaches IMDS, while
the API container stays bridged and still cannot. The security control and the
dashboard both get what they need, and no AWS keys are stored anywhere.

Port 3000 is deliberately not opened in the security group. An unauthenticated
metrics console on the public internet is not a dashboard, it is a
reconnaissance tool.

#### Breaking it on purpose

The rollback path and the detection path are both worth seeing fire.

```bash
# 1. Publish an image that cannot start: point it at training data that is not there.
gh workflow run deploy.yml -f image_tag=sha-<a-known-broken-build>

# or, on the host, simulate the same failure directly:
ssh ec2-user@<host>
docker rm -f loganomaly
docker run -d --name loganomaly --restart unless-stopped -p 8000:8000 \
  -e LOGANOMALY_TRAIN_DATA=/app/data/does-not-exist.csv \
  --log-driver=awslogs --log-opt awslogs-region=<region> \
  --log-opt awslogs-group=/loganomaly/app <image>
```

What to expect, and what to screenshot:

1. **`/health` returns 503**, not 200 — the container is up, the pipeline is not
   fitted, and the gate says so.
2. **The deploy job fails and the rollback fires.** `deploy.sh` exits `1`, the
   workflow summary reads *"Deploy failed — rolled back"*, and the previous image
   is serving again. Exit `1` and exit `2` mean different things; `1` is the one
   you want to see.
3. **`ServerErrors` climbs** in CloudWatch, and `api-5xx-rate` goes to ALARM.
4. **The next self-monitoring run flags the burst.** Force it rather than
   waiting: `sudo systemctl start self-monitor.service && journalctl -u
   self-monitor -n 50`. The flagged windows will carry `"signals": ["rarity"]`
   and templates including `request GET /health server_error`.
5. **`AnomaliesDetected` spikes** on the dashboard and the
   `anomalies-detected` alarm fires — the deployment failure arriving as an
   anomaly in the service's own alert stream.

The verdict and the exact lines that produced it are both in S3 under
`results/` and `logs/app/`, so a finding can be re-examined against its input.

## Roadmap

- [ ] Neural embeddings (`sentence-transformers`) and re-run the comparison — does
      the line-level control still hold at window level?
- [ ] Real LLM backend in `ApiClient.complete()`; measure explanation quality on
      a hand-labelled sample
- [ ] Sequence models (LSTM / transformer over template sequences) — count vectors
      discard ordering entirely, which is likely the next big lever
- [ ] Full BGL (4.7M lines) rather than the 2k sample, for throughput numbers
- [x] Deploy on AWS: S3 for log storage, EC2 for the service — see Stage 3.
      Lambda was the original plan and is the wrong shape here: the pipeline
      fits a detector at startup and holds it in memory, which a function
      that cold-starts per request cannot do without refitting every call.
- [ ] Precision@k at realistic alert budgets (10/day, 100/day) rather than at
      the true anomaly count

## Data

[LogHub](https://github.com/logpai/loghub) — BGL and HDFS 2k samples, included in
`data/`. Full datasets available from the LogHub repository.
