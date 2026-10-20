"""Exercise the C latency replay and its separation from throughput/RSS (#585)."""

from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path

import pytest

import perf_ab

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def latency_children(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("cc") is None or shutil.which("cmake") is None:
        pytest.skip("requires a C compiler and CMake")
    out = tmp_path_factory.mktemp("perf-ab-latency")
    subprocess.run(
        ["cmake", "-S", str(ROOT), "-B", str(out), *perf_ab.FLAGS],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["cmake", "--build", str(out), "--target", "mimalloc-static", "--parallel", "2"],
        check=True,
        capture_output=True,
    )
    for name, defines in (("perf_ab", []), ("perf_ab_latency", ["-DPERF_AB_LATENCY=1"])):
        subprocess.run(
            [
                "cc",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                *defines,
                "-I",
                str(ROOT / "include"),
                str(ROOT / "ci/perf_ab.c"),
                str(out / "libmimalloc.a"),
                "-lpthread",
                "-o",
                str(out / name),
            ],
            check=True,
            capture_output=True,
        )
    return out


def test_c_nearest_rank_and_sampling_across_batches(latency_children: Path) -> None:
    subprocess.run([str(latency_children / "perf_ab_latency"), "latency-selftest"], check=True)


@pytest.mark.parametrize(
    ("generations", "pause", "tables"), [(1, 0, 0), (8, 0, 0), (1, 1, 0), (1, 0, 16)]
)
def test_latency_replay_covers_each_workload_path(
    latency_children: Path, generations: int, pause: int, tables: int
) -> None:
    output = subprocess.run(
        [
            str(latency_children / "perf_ab_latency"),
            "2",
            str(generations),
            "16",
            "1024",
            "10000",
            str(pause),
            str(tables),
            "uniform",
            "8",
            "500",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    latency = float(output)
    assert math.isfinite(latency) and latency > 0


def test_measured_metrics_keep_the_ordinary_child_rss_floor(latency_children: Path) -> None:
    params = perf_ab.Params(2, 1, 16, 1024, 10000, 0, 0)
    values = perf_ab.measure(latency_children / "perf_ab", params, {}, 500)
    assert len(values) == len(perf_ab.METRICS) + 1
    assert values[perf_ab.METRICS.index("p99 operation ns")] > 0
    assert 0 < values[perf_ab.IDEAL_FIELD] < values[perf_ab.METRICS.index("peak RSS MiB")]
