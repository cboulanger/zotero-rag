#!/usr/bin/env python3
"""Compute embedding backend throughput from cron_indexer.log.

Pairs each successful "HTTP Request: POST <url> ... 200 OK" line with the
"[TIMING] embed_batch: call N/M = Xs for Y texts" line that follows it, and
aggregates texts/sec per backend host (and per host+model).

Logs are rotated (see PR #67) into siblings named
`cron_indexer.log.<YYYY-MM-DD_HHMMSS>`, so this accepts multiple paths
and/or glob patterns and processes them all as one combined log.

Usage:
    uv run python scripts/analyze_embedding_throughput.py /path/to/cron_indexer.log
    uv run python scripts/analyze_embedding_throughput.py '/path/to/cron_indexer.log*'
"""

import argparse
import glob
import os
import re
import statistics
import sys
from collections import defaultdict
from urllib.parse import urlparse

HTTP_RE = re.compile(
    r'HTTP Request: POST (https?://\S+) "HTTP/\S+ (\d+)'
)
SUMMARY_RE = re.compile(
    r"embed_batch: \d+ texts .*model=([^)]+)\)"
)
CALL_RE = re.compile(
    r"embed_batch: call (\d+)/(\d+) = ([\d.]+)s for (\d+) texts \(elapsed=([\d.]+)s\)"
)
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


class Stats:
    def __init__(self):
        self.calls = 0
        self.texts = 0
        self.seconds = 0.0
        self.durations = []  # per-call seconds (for median/p90)
        self.rates = []  # per-call texts/sec
        self.first_ts = None
        self.last_ts = None

    def add(self, texts, seconds, ts):
        self.calls += 1
        self.texts += texts
        self.seconds += seconds
        self.durations.append(seconds)
        if seconds > 0:
            self.rates.append(texts / seconds)
        if ts:
            if self.first_ts is None or ts < self.first_ts:
                self.first_ts = ts
            if self.last_ts is None or ts > self.last_ts:
                self.last_ts = ts


def pct(data, p):
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def resolve_logfiles(patterns):
    """Expand glob patterns/literal paths into a deduped list of files,
    sorted oldest-to-newest by mtime (rotated files precede the active log)."""
    paths = set()
    for pattern in patterns:
        matches = glob.glob(pattern)
        if not matches:
            print(f"Warning: no files matched '{pattern}'", file=sys.stderr)
        paths.update(matches)
    if not paths:
        sys.exit("No log files found.")
    return sorted(paths, key=os.path.getmtime)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "logfiles",
        nargs="+",
        help="Path(s) to cron_indexer.log; supports glob patterns "
        "(e.g. 'cron_indexer.log*' for rotated files) — quote patterns "
        "so the shell doesn't expand them first",
    )
    args = parser.parse_args()
    logfiles = resolve_logfiles(args.logfiles)

    by_host = defaultdict(Stats)
    by_host_model = defaultdict(Stats)

    unmatched_calls = 0
    line_count = 0

    # current_model persists across file boundaries: a batch's summary line
    # can land in one rotated file while its "call" lines continue into the
    # next. last_host is reset per file since a trailing HTTP line in one
    # file must not pair with a call line at the start of the next.
    current_model = "unknown"

    for logfile in logfiles:
        last_host = None

        with open(logfile, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line_count += 1

                m = HTTP_RE.search(line)
                if m:
                    url, status = m.group(1), m.group(2)
                    last_host = urlparse(url).netloc if status == "200" else None
                    continue

                m = SUMMARY_RE.search(line)
                if m:
                    current_model = m.group(1)
                    continue

                m = CALL_RE.search(line)
                if m:
                    seconds = float(m.group(3))
                    texts = int(m.group(4))
                    if last_host is None:
                        unmatched_calls += 1
                        continue
                    ts_m = TS_RE.match(line)
                    ts = ts_m.group(1) if ts_m else None
                    by_host[last_host].add(texts, seconds, ts)
                    by_host_model[(last_host, current_model)].add(texts, seconds, ts)
                    # consume the pairing so a stray extra call line doesn't
                    # silently reuse a stale host after a non-200 response
                    continue

    print(f"Parsed {line_count:,} lines across {len(logfiles)} file(s):")
    for logfile in logfiles:
        print(f"  {logfile}")
    if unmatched_calls:
        print(f"Warning: {unmatched_calls:,} call lines had no preceding 200 OK host.\n")

    print("=" * 100)
    print(f"{'Backend host':<35} {'calls':>8} {'texts':>10} {'total_s':>10} "
          f"{'texts/s':>10} {'median_s':>9} {'p90_s':>9} {'active window'}")
    print("-" * 100)
    for host, s in sorted(by_host.items(), key=lambda kv: -kv[1].texts):
        throughput = s.texts / s.seconds if s.seconds else 0.0
        median_s = statistics.median(s.durations) if s.durations else 0.0
        p90_s = pct(s.durations, 0.9)
        window = f"{s.first_ts} .. {s.last_ts}"
        print(f"{host:<35} {s.calls:>8,} {s.texts:>10,} {s.seconds:>10.1f} "
              f"{throughput:>10.2f} {median_s:>9.2f} {p90_s:>9.2f} {window}")

    print()
    print("=" * 100)
    print("Breakdown by host + model")
    print("-" * 100)
    print(f"{'Backend host':<30} {'model':<35} {'calls':>8} {'texts':>10} "
          f"{'total_s':>10} {'texts/s':>10}")
    print("-" * 100)
    for (host, model), s in sorted(by_host_model.items(), key=lambda kv: -kv[1].texts):
        throughput = s.texts / s.seconds if s.seconds else 0.0
        print(f"{host:<30} {model:<35} {s.calls:>8,} {s.texts:>10,} "
              f"{s.seconds:>10.1f} {throughput:>10.2f}")


if __name__ == "__main__":
    main()
