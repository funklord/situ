"""The C++ backend's output must survive Qt's macros.

Qt defines `slots`, `signals` and `emit` as macros, so a generated C++
identifier of one of those names does not mean what it says in any
translation unit that has included a Qt header. That is not this repository
being fussy: three of these projects are Qt, and the drive and converse rungs
are exactly what a Qt consumer of `--layer drive` includes.

The hazard is worse than a compile error would be. A constructor parameter
named `slots` is *deleted* by the macro rather than rejected, so
`slots_(slots)` became `slots_()` -- a null the constructor then wrote
through -- and the whole thing compiled clean under `-Wall -Wextra`. Measured
before the fix: a segfault at construction. The parameter is `store` now.

The guard emulates the macro rather than requiring Qt, so it runs everywhere,
and it was watched failing with the fix reverted -- a regression test for a
bug that compiles clean is worth nothing until it has been seen to catch it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from every_schema import ROOT

CXX     = shutil.which("g++") or shutil.which("clang++")
RUNTIME = ROOT / "runtime"
SCHEMA  = ROOT / "example" / "dns" / "dns.situ"


SLOTS_PROBE = r"""
/* Qt defines `slots` as an empty macro (qtmetamacros.h). Emulated here so
 * the guard needs no Qt: a generated constructor taking `slot *slots` would
 * lose the parameter name, `slots_(slots)` would become `slots_()` -- a null
 * the constructor then writes through -- and it compiles clean. */
#define slots
#define emit

#include <cstdio>
#include "dns_drive.hpp"

struct sink : ::situ::io {
	::situ::rt::err submit(const std::uint8_t *, std::uint32_t) noexcept
		override { return ::situ::rt::err::ok; }
};

int main(void)
{
	sink s;
	::situ::reply_to_driver::slot store[2];
	store[0].live = true;
	store[1].live = true;
	::situ::reply_to_driver drive(store, 2u, s, 40u, 2u);
	/* The constructor clears every slot through the pointer it was given.
	 * Where the macro ate the parameter it wrote through a null instead,
	 * and never reached here. */
	if (store[0].live || store[1].live) {
		std::printf("the constructor did not use the caller's array\n");
		return 1;
	}
	std::printf("SLOTS OK\n");
	return 0;
}
"""


@pytest.mark.skipif(CXX is None, reason="needs a C++ compiler")
def test_the_cpp_layers_survive_qts_slots_macro(tmp_path: Path) -> None:
	"""Qt's `slots` macro must not eat a generated parameter name.

	This guards the C++ *drive and converse* layers, not the Qt driver: any
	Qt consumer of `--layer converse` or `--layer drive` includes those
	headers in a translation unit that has seen Qt, and a constructor
	parameter named `slots` vanishes there. Measured before the fix: the
	member initialiser became `slots_()`, the constructor wrote through a
	null, and it segfaulted at construction having compiled clean under
	`-Wall -Wextra`. Emulating the macro keeps the guard portable, so it
	runs where Qt is not installed.
	"""
	gen = tmp_path / "gen"
	subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(SCHEMA),
		 "--target", "cpp", "--layer", "drive", "--out", str(gen)],
		cwd=ROOT, capture_output=True, text=True, check=True)

	source = tmp_path / "probe.cpp"
	source.write_text(SLOTS_PROBE, encoding="ascii")

	assert CXX is not None
	binary = tmp_path / "probe"
	compiled = subprocess.run(
		[CXX, "-std=c++17", "-O1", "-Wall", "-Wextra",
		 f"-I{gen}", f"-I{RUNTIME / 'cpp'}", f"-I{RUNTIME / 'c'}",
		 str(source), str(RUNTIME / "c" / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert compiled.returncode == 0, compiled.stderr

	ran = subprocess.run([str(binary)], capture_output=True, text=True,
	                     timeout=60)
	assert ran.returncode == 0, ran.stdout + ran.stderr
	assert "SLOTS OK" in ran.stdout, ran.stdout
