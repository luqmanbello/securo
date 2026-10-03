"""Print the backend test files for one CI shard.

Usage: backend_shards.py INDEX TOTAL [TESTS_DIR]

Files are dealt largest-first to the lightest shard, so shards carry similar
amounts of test code. The split is deterministic, and every tests/test_*.py
file lands in exactly one shard (.github/ci/test_backend_shards.py).
"""

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) not in (3, 4):
        print(__doc__, file=sys.stderr)
        return 2
    index, total = int(sys.argv[1]), int(sys.argv[2])
    if total < 1 or not 0 <= index < total:
        print(f"bad shard {index}/{total}", file=sys.stderr)
        return 2
    tests = Path(sys.argv[3] if len(sys.argv) == 4 else "tests")
    files = sorted(tests.glob("test_*.py"), key=lambda p: (-p.stat().st_size, p.name))
    if not files:
        print(f"no test files under {tests}", file=sys.stderr)
        return 2
    loads = [0] * total
    shards: list[list[Path]] = [[] for _ in range(total)]
    for path in files:
        lightest = loads.index(min(loads))
        shards[lightest].append(path)
        loads[lightest] += path.stat().st_size
    print(" ".join(str(p) for p in sorted(shards[index])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
