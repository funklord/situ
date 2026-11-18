"""A registry generates a class in C++ (0070, step 6).

The C registry (26.1081) as a header-only class: a nested `entry`, the
table as a C++17 inline static, and `find`, `check`, `value` and
`safer`, held to the same 33 cases and built under the C++ tests' own
flags -- `-pedantic-errors -fno-exceptions` among them.

Building it found a backend fault no corpus schema reached: a `decimal u64`
over an unbounded span emitted its `_value` parse limit, 2^64 - 1, with no
`u`, which no signed type holds and `-pedantic-errors` refuses.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

from situc.cli import main					# noqa: E402
from test_a_registry_is_generated_in_c import CASES, SCHEMA	# noqa: E402

HOST_CXX = shutil.which("g++") or shutil.which("clang++")
WARNINGS = ["-std=c++17", "-pedantic-errors", "-O1", "-Wall", "-Wextra",
            "-Wconversion", "-Wsign-conversion", "-Werror", "-fno-exceptions",
            "-fno-rtti"]

PROBE = r"""#include <cstdio>
#include <cstring>
#include "settings.hpp"
using reg = situ::node;
static const reg::entry *find(const char *k) { return reg::find(reinterpret_cast<const std::uint8_t *>(k), static_cast<std::uint32_t>(std::strlen(k))); }
int main(int argc, char **argv)
{
	for (int i = 1; i + 2 < argc; i += 3) {
		const char *op = argv[i], *key = argv[i + 1], *arg = argv[i + 2];
		const reg::entry *e = find(key);
		const auto *a = reinterpret_cast<const std::uint8_t *>(arg);
		const auto n = static_cast<std::uint32_t>(std::strlen(arg));
		if (!std::strcmp(op, "find")) { std::printf("%s\n", e ? e->key : "-"); }
		else if (!std::strcmp(op, "check")) { std::printf("%d\n", static_cast<int>(reg::check(*e, a, n))); }
		else if (!std::strcmp(op, "value")) { std::int64_t out = -1; if (reg::value(*e, a, n, out) != situ::rt::err::ok) out = -1; std::printf("%lld\n", static_cast<long long>(out)); }
		else { const char *c = std::strchr(arg, ','); std::printf("%d\n", reg::safer(*e, a, static_cast<std::uint32_t>(c - arg), reinterpret_cast<const std::uint8_t *>(c + 1), static_cast<std::uint32_t>(std::strlen(c + 1))) ? 1 : 0); }
	}
	const reg::entry *g = find("archive/gone");
	std::printf("row %s %s %u %u\n", g->def, g->unit ? g->unit : "-", static_cast<unsigned>(g->scope), static_cast<unsigned>(g->display));
	return 0;
}
"""


@pytest.mark.skipif(HOST_CXX is None, reason="needs a C++ compiler")
def test_the_cpp_registry_answers_what_the_c_one_does(tmp_path: Path) -> None:
	schema = tmp_path / "settings.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	assert main(["build", str(schema), "--target", "cpp",
	             "--out", str(tmp_path)]) == 0
	(tmp_path / "probe.cpp").write_text(PROBE, encoding="ascii")
	binary = tmp_path / "probe"
	built  = subprocess.run(
		[HOST_CXX, *WARNINGS, f"-I{ROOT / 'runtime' / 'c'}",
		 f"-I{ROOT / 'runtime' / 'cpp'}", f"-I{tmp_path}",
		 str(tmp_path / "probe.cpp"), str(ROOT / "runtime" / "c" / "situ.c"),
		 "-o", str(binary)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	argv = [str(binary)]
	for op, key, arg, _ in CASES:
		argv += [op, key, arg]
	said = subprocess.run(argv, capture_output=True, text=True,
	                      check=True).stdout.splitlines()
	assert said[:-1] == [want for *_, want in CASES], [
		(case, got) for case, got in zip(CASES, said) if case[-1] != got]
	assert said[-1] == "row 14d - 1 4"
