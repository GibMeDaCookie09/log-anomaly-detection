"""Feed the service's own logs back through its own detector.

This is the loop the whole project exists to close. The deployment pipeline emits
logs; those logs land in CloudWatch; this script reads them back, fits a detector
on a baseline period, scores the recent period against it, and publishes the
count of flagged windows as a CloudWatch metric - which has an alarm on it, like
any other operational signal. A bad deploy shows up in the tool's own alert
stream.

Run on the host from a systemd timer (see deploy/self-monitor.timer):

    python scripts/self_monitor.py --log-group /loganomaly/app --bucket <bucket>

Or against a local file, which is how the detection logic is exercised without
any AWS involvement at all:

    python scripts/self_monitor.py --from-file sample.log --no-upload
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np

from loganomaly.detect import IsolationForestDetector, TemplateRarityDetector
from loganomaly.explain import Explainer
from loganomaly.ingest import canonicalise_all
from loganomaly.parse import LogParser
from loganomaly.window import count_vectors, make_windows


@dataclass
class Finding:
    window_index: int
    score: float
    threshold: float
    rarity: float
    rarity_threshold: float
    signals: list[str]
    line_ids: list[int]
    templates: list[str]
    explanation: dict | None = field(default=None)


def fetch_cloudwatch(log_group: str, region: str, minutes: int) -> list[str]:
    """Pull raw log messages for the last `minutes`.

    boto3 is imported here rather than at module level so the --from-file path
    runs on a machine with no boto3 and no credentials.
    """
    import boto3

    client = boto3.client("logs", region_name=region)
    start_ms = int((time.time() - minutes * 60) * 1000)

    messages: list[str] = []
    kwargs: dict = {"logGroupName": log_group, "startTime": start_ms}
    while True:
        resp = client.filter_log_events(**kwargs)
        messages.extend(e["message"].rstrip("\n") for e in resp.get("events", []))
        token = resp.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token
    return messages


def split_baseline_recent(lines: list[str], recent_fraction: float) -> tuple[list[str], list[str]]:
    """Older portion trains, newer portion is scored.

    Fitting on the same lines you score is how an unsupervised detector talks
    itself into believing everything is normal: the anomaly ends up inside the
    model of normality. The split has to be chronological.
    """
    cut = int(len(lines) * (1 - recent_fraction))
    return lines[:cut], lines[cut:]


def analyse(
    baseline: list[str],
    recent: list[str],
    window_size: int,
    percentile: float,
    explain: bool,
    similarity: float = 0.7,
) -> tuple[list[Finding], dict]:
    # Two things happen before parsing, both necessary:
    #
    # 1. Canonicalise. Drain3 masks the status code out of a raw JSON access log,
    #    which is the one field separating a healthy request from a failing one -
    #    see loganomaly/ingest.py.
    # 2. Raise the similarity threshold. Drain3's 0.4 default is tuned for long
    #    unstructured log lines; a canonicalised event is four tokens, where one
    #    differing token is a 25% difference and gets absorbed. At 0.4 every
    #    successful request collapses to a single template regardless of path.
    #
    # One parser across both halves so they share a template space - parsing them
    # separately would give the same line different cluster ids in each half, and
    # every recent window would look novel.
    parser = LogParser(similarity_threshold=similarity)
    baseline_parsed = parser.parse(canonicalise_all(baseline))
    recent_parsed = parser.parse(canonicalise_all(recent))
    n_templates = parser.n_templates + 1

    baseline_windows = make_windows(baseline_parsed, size=window_size)
    recent_windows = make_windows(recent_parsed, size=window_size)

    stats: dict = {
        "baseline_lines": len(baseline),
        "recent_lines": len(recent),
        "baseline_windows": len(baseline_windows),
        "recent_windows": len(recent_windows),
        "templates": parser.n_templates,
    }

    if len(baseline_windows) < 2 or not recent_windows:
        stats["skipped"] = "not enough data to fit a baseline"
        return [], stats

    x_baseline = count_vectors(baseline_windows, n_templates=n_templates)
    x_recent = count_vectors(recent_windows, n_templates=n_templates)

    detector = IsolationForestDetector().fit(x_baseline)
    baseline_scores = detector.score(x_baseline)
    recent_scores = detector.score(x_recent)

    # Isolation Forest alone is blind to the signal that matters most here.
    # sklearn splits a node on a random feature between that feature's min and
    # max *within the training data*, so a template that never appeared in the
    # baseline is a dimension of zero variance and no tree can ever split on it.
    # A bad deploy's defining characteristic - log lines nobody has seen before -
    # is therefore invisible to it. Measured on the sample in this repo, the
    # 503 burst scored 0.606 against a baseline maximum of 0.706: less anomalous
    # than ordinary traffic jitter.
    #
    # So run the library's frequency control alongside it. Inverse log-frequency
    # scores an unseen template highly by construction, which is precisely the
    # case the forest cannot see. A window is flagged if either signal fires.
    rarity = TemplateRarityDetector().fit_ids([p.cluster_id for p in baseline_parsed])
    baseline_rarity = np.array(
        [float(rarity.score_ids(w.cluster_ids).mean()) for w in baseline_windows]
    )
    recent_rarity = np.array(
        [float(rarity.score_ids(w.cluster_ids).mean()) for w in recent_windows]
    )

    # The threshold comes from the baseline's own score distribution rather than
    # a hardcoded constant, so it adapts to how noisy this service normally is.
    threshold = float(np.percentile(baseline_scores, percentile))
    stats.update(
        {
            "threshold": round(threshold, 4),
            "threshold_percentile": percentile,
            "recent_score_max": round(float(recent_scores.max()), 4),
            "baseline_score_spread": round(float(np.ptp(baseline_scores)), 6),
            "rarity_threshold": round(float(np.percentile(baseline_rarity, percentile)), 4),
            "recent_rarity_max": round(float(recent_rarity.max()), 4),
        }
    )

    # A perfectly uniform baseline gives Isolation Forest nothing to split on, so
    # every point - normal or not - gets an identical score and the detector
    # silently stops detecting. Say so rather than reporting a confident zero.
    if float(np.ptp(baseline_scores)) < 1e-9:
        stats["degenerate_baseline"] = (
            "all baseline windows scored identically; the detector cannot "
            "discriminate. Widen --minutes or lower --window-size."
        )

    explainer = Explainer() if explain else None
    normal_templates = list(dict.fromkeys(t for w in baseline_windows for t in w.templates))

    rarity_threshold = float(np.percentile(baseline_rarity, percentile))

    findings: list[Finding] = []
    for window, score, rar in zip(recent_windows, recent_scores, recent_rarity, strict=True):
        signals = []
        if score > threshold:
            signals.append("isolation")
        if rar > rarity_threshold:
            signals.append("rarity")
        if not signals:
            continue

        explanation = None
        if explainer is not None:
            explanation = explainer.explain(window, normal_templates, float(score)).as_dict()
        findings.append(
            Finding(
                window_index=window.index,
                score=round(float(score), 4),
                threshold=round(threshold, 4),
                rarity=round(float(rar), 4),
                rarity_threshold=round(rarity_threshold, 4),
                signals=signals,
                line_ids=window.line_ids,
                templates=list(dict.fromkeys(window.templates))[:10],
                explanation=explanation,
            )
        )

    # Rank by how far past its own threshold each signal went, so a window that
    # is wildly novel outranks one that is marginally odd.
    findings.sort(key=lambda f: -max(f.score - f.threshold, f.rarity - f.rarity_threshold))
    return findings, stats


def upload(bucket: str, key: str, body: str, region: str, content_type: str) -> None:
    import boto3

    boto3.client("s3", region_name=region).put_object(
        Bucket=bucket, Key=key, Body=body.encode(), ContentType=content_type
    )


def publish_metric(namespace: str, count: int, region: str) -> None:
    import boto3

    boto3.client("cloudwatch", region_name=region).put_metric_data(
        Namespace=namespace,
        MetricData=[{"MetricName": "AnomaliesDetected", "Value": count, "Unit": "Count"}],
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Run the detector over the service's own logs.")
    ap.add_argument("--log-group", default="/loganomaly/app")
    ap.add_argument("--bucket", default="")
    ap.add_argument("--region", default="ap-south-1")
    ap.add_argument("--namespace", default="loganomaly/api")
    ap.add_argument("--minutes", type=int, default=120, help="Total lookback window.")
    ap.add_argument(
        "--recent-fraction",
        type=float,
        default=0.25,
        help="Trailing share of the lookback that is scored; the rest is the baseline.",
    )
    ap.add_argument("--window-size", type=int, default=20)
    ap.add_argument(
        "--similarity",
        type=float,
        default=0.7,
        help="Drain3 similarity threshold. Higher splits more; 0.4 (the library "
        "default) is too permissive for short canonicalised events.",
    )
    ap.add_argument(
        "--percentile",
        type=float,
        default=95.0,
        help="Baseline score percentile above which a recent window is flagged.",
    )
    ap.add_argument("--from-file", default="", help="Read lines from a file instead of CloudWatch.")
    ap.add_argument("--no-upload", action="store_true", help="Skip S3 and CloudWatch writes.")
    ap.add_argument("--no-explain", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.from_file:
        with open(args.from_file, encoding="utf-8", errors="replace") as fh:
            lines = [ln.rstrip("\n") for ln in fh if ln.strip()]
        source = args.from_file
    else:
        lines = fetch_cloudwatch(args.log_group, args.region, args.minutes)
        source = f"cloudwatch:{args.log_group}"

    now = datetime.now(UTC)
    baseline, recent = split_baseline_recent(lines, args.recent_fraction)
    findings, stats = analyse(
        baseline,
        recent,
        args.window_size,
        args.percentile,
        explain=not args.no_explain,
        similarity=args.similarity,
    )

    report = {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": "self_monitor",
        "source": source,
        "stats": stats,
        "anomalies_detected": len(findings),
        "findings": [f.__dict__ for f in findings],
    }

    # This summary goes to stdout, which the awslogs driver forwards to the same
    # log group the detector reads back - so its own runs become part of the
    # record it later examines.
    print(json.dumps({k: report[k] for k in ("ts", "event", "source", "stats")}))
    print(json.dumps({"event": "self_monitor_result", "anomalies_detected": len(findings)}))
    for finding in findings[:5]:
        summary = (finding.explanation or {}).get("summary", "")
        print(f"  window {finding.window_index}  score={finding.score}  {summary}")

    if not args.no_upload and args.bucket:
        stamp = now.strftime("%Y/%m/%d/%H%M%S")
        upload(
            args.bucket,
            f"results/{stamp}.json",
            json.dumps(report, indent=2),
            args.region,
            "application/json",
        )
        # Archive the raw batch alongside the verdict, so a finding can be
        # re-examined against the exact input that produced it.
        upload(args.bucket, f"logs/app/{stamp}.log", "\n".join(lines), args.region, "text/plain")
        publish_metric(args.namespace, len(findings), args.region)

    return 0


if __name__ == "__main__":
    sys.exit(main())
