#!/usr/bin/env python3
"""Run the same 30 synthetic requests before and after a reranker hardware change."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import time
from urllib.request import Request, urlopen


FIXTURES = [
    ("Which service stores time series metrics?", [
        "Prometheus stores time series metrics and evaluates alert rules.",
        "VictoriaLogs stores application logs and supports aggregate log queries.",
        "Flux reconciles Kubernetes manifests from Git repositories.",
        "Ceph provides persistent block and filesystem storage.",
        "Kanidm authenticates users and issues OpenID Connect tokens.",
    ]),
    ("How should an unavailable Kubernetes workload be investigated?", [
        "Inspect the Deployment conditions, pod events, readiness probes, and recent error logs before changing the workload.",
        "For a failed model download, verify the registry, DNS, TLS, and expected checksum before retrying the transfer.",
        "A persistent volume keeps application data when a container restarts. Its storage class chooses the storage backend.",
        "Model inference uses an HTTP endpoint. A single inference slot queues requests while an earlier request is processing.",
        "The morning calendar brief lists upcoming meetings, unfinished household actions, and approaching deadlines.",
    ]),
    ("Why can inference requests time out despite a healthy model server?", [
        "A server may pass its health check while its single inference slot is occupied. Long prompts and concurrent clients queue behind active generation, so end-to-end latency can exceed the client deadline.",
        "An HTTP readiness probe indicates that a server can accept connections. It does not guarantee that a queued request will complete within a particular deadline.",
        "A CPU reranker scores query and document pairs. Ranking order and tail latency should be checked on a fixed fixture after changing its hardware allocation.",
        "A checksum verifies that a downloaded model matches the expected file. An invalid checksum must prevent loading the artifact.",
        "A scheduled log review compares bounded aggregate counts against the preceding interval and records incomplete coverage explicitly.",
    ]),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Reranker base URL, typically a local port-forward")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    digest = hashlib.sha256(json.dumps(FIXTURES).encode()).hexdigest()
    baseline = json.loads(args.baseline.read_text()) if args.baseline else None
    if baseline:
        assert baseline["fixtures_sha256"] == digest
        assert len(baseline["requests"]) == 30 and baseline["passed"]
    results = []
    for index in range(30):
        query, documents = FIXTURES[index % len(FIXTURES)]
        body = {"model": "qwen3-reranker-0.6b", "query": query,
                "documents": documents, "top_n": len(documents)}
        start = time.monotonic()
        result = {"fixture": index % len(FIXTURES)}
        try:
            request = Request(args.url.rstrip("/") + "/v1/rerank", data=json.dumps(body).encode(),
                              headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=30) as response:
                ranked = json.load(response)["results"]
            result["ranking"] = [row["index"] for row in ranked]
            result["scores"] = [row["relevance_score"] for row in ranked]
            assert sorted(result["ranking"]) == list(range(len(documents)))
            assert all(math.isfinite(score) for score in result["scores"])
            assert result["ranking"][0] == 0
            if baseline:
                assert result["ranking"] == baseline["requests"][index]["ranking"]
        except Exception as exc:
            result["error_type"] = type(exc).__name__
        result["seconds"] = time.monotonic() - start
        results.append(result)
    p95 = sorted(row["seconds"] for row in results)[math.ceil(len(results) * .95) - 1]
    errors = sum("error_type" in row for row in results)
    report = {"fixtures_sha256": digest, "requests": results, "p95_seconds": p95,
              "errors": errors, "passed": errors == 0 and p95 < 5}
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "requests"}))
    assert report["passed"], "Reranker gate failed; inspect the report before cutover"


if __name__ == "__main__":
    main()
