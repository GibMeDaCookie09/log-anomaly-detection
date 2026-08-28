# Evidence: a deliberately broken deploy

Captured 2026-08-28 from a real run against live AWS infrastructure. Every file
here is raw command output. Nothing was edited by hand.

| File | What it shows |
|---|---|
| `01-healthy-before.json` | `/health` on the good build, reporting the commit SHA it was built from |
| `02-broken-health.json` | `/health` on the broken build — **HTTP 503**, naming the file it could not find |
| `03-deploy-rollback.log` | `deploy.sh` detecting a SHA mismatch, dumping context, restoring the previous image, **exit 1** |
| `04-workflow-result.json` | The full pipeline: 7 CI jobs, build/push, and the deploy, all green |
| `05-alarm-state.json` | CloudWatch `api-5xx-rate` transitioning **OK → ALARM** under sustained failure |
| `06-detector-finding.log` | The detector flagging the failure burst in the service's own logs |
| `07-healthy-after.json` | `/health` after recovery, back on the good build |

## What each one demonstrates

**The health gate can fail.** `02` is a 503, not a 200 with a status field. It
names the missing file and reports the build. A gate that cannot go red is not a
gate.

**The rollback works.** In `03` the new container came up *healthy* — but reported
a different commit than the one being deployed. That is the failure mode a plain
liveness check misses entirely: the deploy would have reported success having
shipped nothing. The SHA check caught it, the previous image was restored, and
the script exited `1` — "the change did not ship, but the service is up" — rather
than `2`, which means nobody is serving traffic.

**The alarm needs two periods.** `05` shows real OK → ALARM transitions. An
earlier, shorter burst hit a 28.8% error rate in a single period and correctly
did *not* fire, because the alarm requires two consecutive breaching periods.
That is the anti-noise design working, not a miss.

**Detection is relative, and the log shows both sides of that.** `06` contains two
runs. The first, against five minutes of clean baseline followed by a burst,
flags the burst — rarity `1.2346` against a `0.0064` threshold. The second, after
90 minutes of sustained failure, reports **zero** — correctly, because by then the
failure *is* the baseline and there is no contrast left to measure. Anomaly
detection answers "different from usual", not "bad".

## Not captured here

Screenshots of the GitHub Actions UI and the Grafana dashboard. Those need a
logged-in browser; the underlying data is in `04` and `05` instead, which is
verifiable in a way a screenshot is not.
