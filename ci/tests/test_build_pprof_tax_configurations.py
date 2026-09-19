from __future__ import annotations

# The production script is intentionally standalone, not an installed package.
# ruff: noqa: I001

import unittest
from pathlib import Path
from typing import Any

import build_benchmark_allocators as allocators
import build_pprof_tax_configurations as builder

SOURCE_DIR = Path("/src/pprof-tax")
BUILD_DIR = Path("/build/pprof-tax/upstream-baseline")
C_COMPILER = "/usr/bin/cc"

COMMON_CMAKE_PREFIX: list[str] = [
    "cmake",
    "-S",
    str(SOURCE_DIR),
    "-B",
    str(BUILD_DIR),
    "-G",
    "Ninja",
    "-DCMAKE_BUILD_TYPE=Release",
    "-DMI_BUILD_STATIC=ON",
    "-DMI_BUILD_SHARED=OFF",
    "-DMI_BUILD_TESTS=OFF",
    "-DMI_OPT_ARCH=OFF",
    "-DMI_OPT_SIMD=ON",
    f"-DCMAKE_C_COMPILER={C_COMPILER}",
]


def cache_with(**overrides: object) -> dict[str, Any]:
    """A cache projection where every EQUIVALENCE_CACHE_KEYS entry agrees, except
    whatever `overrides` names -- the baseline `check_equivalence` fixture tests
    mutate one key at a time away from."""
    merged: dict[str, Any] = dict.fromkeys(builder.EQUIVALENCE_CACHE_KEYS, "SAME")
    merged["CMAKE_C_FLAGS_RELEASE"] = "-O3"
    merged.update(overrides)
    return merged


def consistent_configurations() -> list[dict[str, Any]]:
    """Four made-up but internally consistent configurations: same compiler, same
    linker, every cache key equal apart from the one knob each pair is allowed to
    differ in. `check_equivalence` must accept this set unmodified."""
    identity = {"c_compiler_identity": "cc (Ubuntu) 13.2.0", "linker_identity": "GNU ld 2.42"}
    return [
        {
            **identity,
            "compiled_configuration_id": "upstream-baseline",
            "source_sha": "upstream-sha",
            "cmake_cache": cache_with(MI_PPROF=None, MI_DHAT=None),
        },
        {
            **identity,
            "compiled_configuration_id": "fork-pprof-off",
            "source_sha": "fork-sha",
            "cmake_cache": cache_with(MI_PPROF="OFF"),
        },
        {
            **identity,
            "compiled_configuration_id": "fork-pprof-on",
            "source_sha": "fork-sha",
            "cmake_cache": cache_with(MI_PPROF="ON"),
        },
        {
            **identity,
            "compiled_configuration_id": "fork-pprof-off-frame-pointers",
            "source_sha": "fork-sha",
            "cmake_cache": cache_with(
                MI_PPROF="OFF", CMAKE_C_FLAGS_RELEASE="-O3 -fno-omit-frame-pointer"
            ),
        },
    ]


def entry_for(configurations: list[dict[str, Any]], compiled_id: str) -> dict[str, Any]:
    return next(e for e in configurations if e["compiled_configuration_id"] == compiled_id)


class CmakeArgumentsTests(unittest.TestCase):
    def test_upstream_baseline_carries_no_mi_pprof_flag(self) -> None:
        self.assertEqual(
            [*COMMON_CMAKE_PREFIX, "-DCMAKE_C_FLAGS_RELEASE=-O3"],
            builder.cmake_arguments("upstream-baseline", SOURCE_DIR, BUILD_DIR, C_COMPILER),
        )

    def test_fork_pprof_off_is_the_common_prefix_plus_mi_pprof_off(self) -> None:
        self.assertEqual(
            [*COMMON_CMAKE_PREFIX, "-DMI_PPROF=OFF", "-DCMAKE_C_FLAGS_RELEASE=-O3"],
            builder.cmake_arguments("fork-pprof-off", SOURCE_DIR, BUILD_DIR, C_COMPILER),
        )

    def test_fork_pprof_on_is_the_common_prefix_plus_mi_pprof_on(self) -> None:
        self.assertEqual(
            [*COMMON_CMAKE_PREFIX, "-DMI_PPROF=ON", "-DCMAKE_C_FLAGS_RELEASE=-O3"],
            builder.cmake_arguments("fork-pprof-on", SOURCE_DIR, BUILD_DIR, C_COMPILER),
        )

    def test_fork_pprof_off_frame_pointers_forces_the_flag_by_hand(self) -> None:
        self.assertEqual(
            [
                *COMMON_CMAKE_PREFIX,
                "-DMI_PPROF=OFF",
                "-DCMAKE_C_FLAGS_RELEASE=-O3 -fno-omit-frame-pointer",
            ],
            builder.cmake_arguments(
                "fork-pprof-off-frame-pointers", SOURCE_DIR, BUILD_DIR, C_COMPILER
            ),
        )

    def test_the_frame_pointer_flag_appears_in_no_other_configuration(self) -> None:
        for compiled_id in ("upstream-baseline", "fork-pprof-off", "fork-pprof-on"):
            args = builder.cmake_arguments(compiled_id, SOURCE_DIR, BUILD_DIR, C_COMPILER)
            self.assertTrue(
                all("-fno-omit-frame-pointer" not in arg for arg in args),
                f"{compiled_id} must not carry the frame-pointer flag: {args}",
            )

    def test_rejects_an_unknown_configuration_id(self) -> None:
        with self.assertRaises(builder.BuildError):
            builder.cmake_arguments("not-a-real-configuration", SOURCE_DIR, BUILD_DIR, C_COMPILER)


class ParseCmakeCacheTests(unittest.TestCase):
    def test_drops_comments_and_blank_lines_and_keeps_internal_entries(self) -> None:
        text = (
            "# This is the CMakeCache file.\n"
            "# For build in directory: /build/pprof-tax\n"
            "\n"
            "// Enable additional documentation\n"
            "//Another comment style, no space\n"
            "\n"
            "MI_PPROF:BOOL=ON\n"
            "CMAKE_C_FLAGS_RELEASE:STRING=-O3 -fno-omit-frame-pointer\n"
            "MI_OPT_ARCH:INTERNAL=OFF\n"
        )
        self.assertEqual(
            {
                "MI_PPROF": "ON",
                "CMAKE_C_FLAGS_RELEASE": "-O3 -fno-omit-frame-pointer",
                "MI_OPT_ARCH": "OFF",
            },
            builder.parse_cmake_cache(text),
        )

    def test_an_empty_cache_parses_to_an_empty_dict(self) -> None:
        self.assertEqual({}, builder.parse_cmake_cache("# only comments\n// and more\n"))

    def test_a_value_containing_an_equals_sign_is_kept_whole(self) -> None:
        cache = builder.parse_cmake_cache("CMAKE_EXE_LINKER_FLAGS:STRING=-Wl,--defsym=x=1\n")
        self.assertEqual({"CMAKE_EXE_LINKER_FLAGS": "-Wl,--defsym=x=1"}, cache)


class ProjectCacheTests(unittest.TestCase):
    def test_projects_every_equivalence_key_defaulting_missing_ones_to_none(self) -> None:
        cache = {"MI_PPROF": "OFF"}
        projected = builder.project_cache(cache, "fork-pprof-off")
        self.assertEqual(set(builder.EQUIVALENCE_CACHE_KEYS), set(projected.keys()))
        self.assertEqual("OFF", projected["MI_PPROF"])
        self.assertIsNone(projected["MI_DHAT"])

    def test_upstream_baseline_nulls_mi_pprof_and_mi_dhat_even_if_present(self) -> None:
        cache = {"MI_PPROF": "OFF", "MI_DHAT": "ON", "MI_OPT_ARCH": "OFF"}
        projected = builder.project_cache(cache, "upstream-baseline")
        self.assertIsNone(projected["MI_PPROF"])
        self.assertIsNone(projected["MI_DHAT"])
        self.assertEqual("OFF", projected["MI_OPT_ARCH"])

    def test_a_fork_configuration_keeps_its_real_mi_pprof_value(self) -> None:
        projected = builder.project_cache({"MI_PPROF": "ON"}, "fork-pprof-on")
        self.assertEqual("ON", projected["MI_PPROF"])


class CheckEquivalenceTests(unittest.TestCase):
    def test_accepts_a_planted_consistent_set(self) -> None:
        builder.check_equivalence(consistent_configurations())

    def test_rejects_an_mi_opt_arch_drift_between_fork_configurations(self) -> None:
        configurations = consistent_configurations()
        entry_for(configurations, "fork-pprof-on")["cmake_cache"]["MI_OPT_ARCH"] = "ON"
        with self.assertRaises(builder.EquivalenceError):
            builder.check_equivalence(configurations)

    def test_rejects_a_cmake_c_flags_release_drift_on_fork_pprof_on(self) -> None:
        configurations = consistent_configurations()
        entry_for(configurations, "fork-pprof-on")["cmake_cache"]["CMAKE_C_FLAGS_RELEASE"] = "-O2"
        with self.assertRaises(builder.EquivalenceError):
            builder.check_equivalence(configurations)

    def test_rejects_a_second_fork_source_sha(self) -> None:
        configurations = consistent_configurations()
        entry_for(configurations, "fork-pprof-on")["source_sha"] = "a-different-fork-sha"
        with self.assertRaises(builder.EquivalenceError):
            builder.check_equivalence(configurations)

    def test_rejects_a_compiler_identity_mismatch(self) -> None:
        configurations = consistent_configurations()
        entry_for(configurations, "fork-pprof-on")["c_compiler_identity"] = "clang version 18"
        with self.assertRaises(builder.EquivalenceError):
            builder.check_equivalence(configurations)

    def test_rejects_a_linker_identity_mismatch(self) -> None:
        configurations = consistent_configurations()
        entry_for(configurations, "upstream-baseline")["linker_identity"] = "LLD 18"
        with self.assertRaises(builder.EquivalenceError):
            builder.check_equivalence(configurations)

    def test_rejects_a_frame_pointer_value_that_is_not_off_plus_exactly_one_flag(self) -> None:
        configurations = consistent_configurations()
        fp_cache = entry_for(configurations, "fork-pprof-off-frame-pointers")["cmake_cache"]
        fp_cache["CMAKE_C_FLAGS_RELEASE"] = "-O3 -fno-omit-frame-pointer -O2"
        with self.assertRaises(builder.EquivalenceError):
            builder.check_equivalence(configurations)

    def test_rejects_a_missing_configuration(self) -> None:
        configurations = [
            e
            for e in consistent_configurations()
            if e["compiled_configuration_id"] != "fork-pprof-on"
        ]
        with self.assertRaises(builder.EquivalenceError):
            builder.check_equivalence(configurations)

    def test_rejects_an_upstream_cache_drift_outside_mi_pprof_and_mi_dhat(self) -> None:
        configurations = consistent_configurations()
        entry_for(configurations, "upstream-baseline")["cmake_cache"]["MI_SECURE"] = "ON"
        with self.assertRaises(builder.EquivalenceError):
            builder.check_equivalence(configurations)


class ValidateIdentityProbeTests(unittest.TestCase):
    def expected(self) -> dict[str, Any]:
        return {
            "configuration_id": "fork-pprof-on",
            "allocator_id": "mimalloc-pprof",
            "allocator_version": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            "source_sha": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            "library_sha256": "a" * 64,
            "executable_sha256": "b" * 64,
            "pprof_compiled": True,
        }

    def good_probe(self) -> dict[str, Any]:
        return {**self.expected(), "pprof_enabled": False}

    def test_accepts_a_matching_probe(self) -> None:
        builder.validate_identity_probe(self.good_probe(), self.expected())

    def test_rejects_a_wrong_configuration_id(self) -> None:
        probe = {**self.good_probe(), "configuration_id": "fork-pprof-off"}
        with self.assertRaises(builder.BuildError):
            builder.validate_identity_probe(probe, self.expected())

    def test_rejects_a_wrong_executable_sha(self) -> None:
        probe = {**self.good_probe(), "executable_sha256": "c" * 64}
        with self.assertRaises(builder.BuildError):
            builder.validate_identity_probe(probe, self.expected())

    def test_rejects_a_wrong_library_sha(self) -> None:
        probe = {**self.good_probe(), "library_sha256": "d" * 64}
        with self.assertRaises(builder.BuildError):
            builder.validate_identity_probe(probe, self.expected())

    def test_rejects_pprof_enabled_true(self) -> None:
        probe = {**self.good_probe(), "pprof_enabled": True}
        with self.assertRaises(builder.BuildError):
            builder.validate_identity_probe(probe, self.expected())

    def test_rejects_an_extra_key(self) -> None:
        probe = {**self.good_probe(), "unexpected": "value"}
        with self.assertRaises(builder.BuildError):
            builder.validate_identity_probe(probe, self.expected())

    def test_rejects_a_missing_key(self) -> None:
        probe = self.good_probe()
        del probe["pprof_compiled"]
        with self.assertRaises(builder.BuildError):
            builder.validate_identity_probe(probe, self.expected())

    def test_rejects_a_non_object_probe(self) -> None:
        with self.assertRaises(builder.BuildError):
            builder.validate_identity_probe(["not", "an", "object"], self.expected())


class LockfileUpstreamCommitTests(unittest.TestCase):
    def test_lockfile_upstream_mimalloc_commit_matches_upstream_commit(self) -> None:
        records = allocators.read_lockfile(allocators.default_lockfile())
        upstream = next(r for r in records if r["id"] == "upstream-mimalloc")
        source = allocators.require_mapping(upstream["source"], "upstream-mimalloc.source")
        self.assertEqual(builder.UPSTREAM_COMMIT, source.get("commit"))
        self.assertEqual(40, len(builder.UPSTREAM_COMMIT))


class CompiledConfigurationConstantsTests(unittest.TestCase):
    def test_canonical_order_has_exactly_the_four_documented_configurations(self) -> None:
        self.assertEqual(
            (
                "upstream-baseline",
                "fork-pprof-off",
                "fork-pprof-on",
                "fork-pprof-off-frame-pointers",
            ),
            builder.COMPILED_CONFIGURATION_IDS,
        )

    def test_every_configuration_has_an_allocator_id_and_a_pprof_compiled_flag(self) -> None:
        for compiled_id in builder.COMPILED_CONFIGURATION_IDS:
            self.assertIn(compiled_id, builder.CONFIGURATION_ALLOCATOR_ID)
            self.assertIn(compiled_id, builder.CONFIGURATION_PPROF_COMPILED)
            self.assertIn(compiled_id, builder.CONFIGURATION_FRAME_POINTER_POLICY)

    def test_only_fork_pprof_on_is_pprof_compiled(self) -> None:
        for compiled_id in builder.COMPILED_CONFIGURATION_IDS:
            self.assertEqual(
                compiled_id == "fork-pprof-on",
                builder.CONFIGURATION_PPROF_COMPILED[compiled_id],
            )


if __name__ == "__main__":
    unittest.main()
