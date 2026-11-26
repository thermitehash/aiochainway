"""Print the mutmut score for the last run and fail below a threshold.

Usage: python script/mutation_score.py [min_percent]
"""

from __future__ import annotations

import collections
import subprocess
import sys


def main() -> int:
    threshold = float(sys.argv[1]) if len(sys.argv) > 1 else 85.0
    out = subprocess.run(
        ["mutmut", "results", "--all", "true"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    counts: collections.Counter[str] = collections.Counter()
    for line in out.splitlines():
        if ":" in line:
            counts[line.rsplit(":", 1)[1].strip()] += 1
    killed = counts["killed"] + counts["timeout"]
    total = sum(counts.values()) - counts["skipped"] - counts["not checked"]
    score = 100.0 * killed / total if total else 0.0
    print(f"mutants: {dict(counts)}")
    print(
        f"mutation score: {score:.1f}% (killed {killed} of {total}; threshold {threshold}%)"
    )
    return 0 if score >= threshold else 1


if __name__ == "__main__":
    sys.exit(main())
