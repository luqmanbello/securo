"""Run with python3 .github/ci/test_backend_shards.py; no application dependencies needed."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("backend_shards.py").resolve()


def shard(tests_dir: Path, index: int, total: int) -> list[str]:
    out = subprocess.check_output(
        [sys.executable, str(SCRIPT), str(index), str(total), str(tests_dir)], text=True
    )
    return out.split()


class BackendShardsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tests = Path(self.temp.name) / "tests"
        self.tests.mkdir()
        for i, size in enumerate([900, 50, 400, 400, 10, 300, 700, 5]):
            (self.tests / f"test_m{i}.py").write_text("x" * size)
        (self.tests / "conftest.py").write_text("x")
        (self.tests / "helpers.py").write_text("x")

    def test_every_test_file_is_in_exactly_one_shard(self):
        shards = [shard(self.tests, i, 4) for i in range(4)]
        flat = [f for s in shards for f in s]
        expected = sorted(str(p) for p in self.tests.glob("test_*.py"))
        self.assertEqual(sorted(flat), expected)
        self.assertEqual(len(flat), len(set(flat)))

    def test_shards_are_balanced_by_size(self):
        loads = [sum(Path(f).stat().st_size for f in shard(self.tests, i, 4)) for i in range(4)]
        self.assertLessEqual(max(loads) - min(loads), 900)
        self.assertTrue(all(loads))

    def test_split_is_deterministic(self):
        self.assertEqual(shard(self.tests, 2, 4), shard(self.tests, 2, 4))

    def test_bad_arguments_fail(self):
        for args in (["4", "4"], ["-1", "4"], ["0", "0"]):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, str(SCRIPT), *args, str(self.tests)], capture_output=True)
                self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
