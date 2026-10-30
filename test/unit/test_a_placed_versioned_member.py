"""A versioned member the message places is guarded both ways (26.770).

    struct varv [version = ver] { u8 ver; u8 n; u8 d[n]; u16 x [since = 2]; }

`x` sits where `n` says. Its accessors asked the version, and the bounds
check 26.746 added behind it covers only a member at a constant offset --
so a whole version-2 message that stops after `d` (it says it carries `x`
and does not) was read past by C's getter and written past by C's and
C++'s setters, under ASan. And Rust took its dynamic-offset setter before
its versioned one, so `set_x` on a version-1 message wrote into whatever
followed it. 26.73 settled what a versioned member at a dynamic offset
does where it does not fit -- what every other one does (26.27): the
getter answers zero and the setter writes nothing. The version is asked
first, so an older message is still refused.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import fourway

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from situc.codegen.c import generate as generate_c	# noqa: E402
from situc.codegen.cpp import generate as generate_cpp	# noqa: E402
from situc.codegen.python import generate as generate_py	# noqa: E402
from situc.codegen.rust import generate as generate_rs	# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.parser import parse_text			# noqa: E402
from situc.resolve import resolve			# noqa: E402

HOST_CC  = shutil.which("cc") or shutil.which("gcc")
HOST_CXX = shutil.which("g++") or shutil.which("clang++")
SANITIZE = ["-g", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"]

SCHEMA = """target buffer;
endian big;
struct varv [version = ver] {
	u8   ver;
	u8   n;
	u8   d[n];
	u16  x [since = 2];
}
"""

#: (message, what the setter returns, what the getter returns, the buffer
#: after `x = 0x1234`). Every buffer is exactly the message's length.
CASES = [
	("01020506",   "version", "version", "01020506"),	# not carried
	("02020506",   "ok",      "ok 0",    "02020506"),	# claimed, absent
	("0209",       "ok",      "ok 0",    "0209"),		# `d` overruns
	("020205060000", "ok",    "ok 0",    "020205061234"),
]


def _built() -> tuple[object, object]:
	schema = parse_text(SCHEMA)
	return schema, resolve(schema, solve(schema))


def _cases_c() -> str:
	rows = []
	for message, _, _, _ in CASES:
		raw = ", ".join(f"0x{one:02x}" for one in bytes.fromhex(message))
		rows.append(f"\tone((const uint8_t[]){{{raw}}}, {len(message) // 2});")
	return "\n".join(rows)


C_PROBE = """#include "unit.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static void one(const uint8_t *bytes, uint32_t length)
{
	uint8_t *p = malloc(length);
	situ_msg_t m;
	situ_view_t view;
	uint16_t got = 7;
	memcpy(p, bytes, length);
	situ_msg_init(&m, p, length);
	if (situ_varv_view(&m, 0, length, &view) != SITU_OK) {
		printf("noview\\n");
		free(p);
		return;
	}
	const situ_err_t read = situ_varv_x_get(view, &got);
	const situ_err_t wrote = situ_varv_x_set(view, 0x1234);
	printf("%s|%s|", wrote == SITU_ERR_VERSION ? "version" : wrote == SITU_OK ? "ok" : "other",
	       read == SITU_ERR_VERSION ? "version" : read == SITU_OK ? "ok" : "other");
	if (read == SITU_OK) {
		printf("%u", got);
	}
	printf("|");
	for (uint32_t i = 0; i < length; i++) {
		printf("%02x", p[i]);
	}
	printf("\\n");
	free(p);
}
int main(void)
{
CASES
	return 0;
}
"""

CPP_PROBE = """#include "unit.hpp"
#include <cstdio>
#include <cstdlib>
#include <cstring>
static void one(const std::uint8_t *bytes, std::uint32_t length)
{
	auto *p = static_cast<std::uint8_t *>(std::malloc(length));
	std::memcpy(p, bytes, length);
	::situ::rt::message m(p, length);
	::situ::varv view;
	if (::situ::varv::at(m, 0, length, view) != ::situ::rt::err::ok) {
		std::printf("noview\\n");
		std::free(p);
		return;
	}
	std::uint16_t got = 7;
	const auto read = view.x(got);
	const auto wrote = view.set_x(0x1234);
	auto name = [](::situ::rt::err e) {
		return e == ::situ::rt::err::version ? "version"
		     : e == ::situ::rt::err::ok ? "ok" : "other";
	};
	std::printf("%s|%s|", name(wrote), name(read));
	if (read == ::situ::rt::err::ok) {
		std::printf("%u", static_cast<unsigned>(got));
	}
	std::printf("|");
	for (std::uint32_t i = 0; i < length; i++) {
		std::printf("%02x", p[i]);
	}
	std::printf("\\n");
	std::free(p);
}
int main()
{
CASES
	return 0;
}
"""


def _expected() -> list[str]:
	return [f"{wrote}|{read.split()[0]}|{' '.join(read.split()[1:])}|{after}"
	        for _, wrote, read, after in CASES]


def _run(command: list[str], binary: Path) -> list[str]:
	made = subprocess.run(command, capture_output=True, text=True)
	assert made.returncode == 0, made.stderr[-2000:]
	ran = subprocess.run([str(binary)], capture_output=True, text=True)
	assert ran.returncode == 0, ran.stderr[-2000:]
	return ran.stdout.split()


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_c_stays_in_the_frame(tmp_path: Path) -> None:
	schema, resolved = _built()
	built = generate_c(schema, resolved, "unit")	# type: ignore[arg-type]
	(tmp_path / "unit.h").write_text(built.header, encoding="ascii")
	(tmp_path / "unit.c").write_text(built.source, encoding="ascii")
	(tmp_path / "main.c").write_text(C_PROBE.replace("CASES", _cases_c()),
	                                 encoding="ascii")
	given = _run([HOST_CC or "cc", "-std=c11", *SANITIZE,
	              f"-I{ROOT / 'runtime' / 'c'}", f"-I{tmp_path}",
	              str(tmp_path / "main.c"), str(tmp_path / "unit.c"),
	              str(ROOT / "runtime" / "c" / "situ.c"),
	              "-o", str(tmp_path / "bin")], tmp_path / "bin")
	assert given == _expected(), given


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_cpp_stays_in_the_frame(tmp_path: Path) -> None:
	schema, resolved = _built()
	(tmp_path / "unit.hpp").write_text(
		generate_cpp(schema, resolved, "unit").header,	# type: ignore[arg-type]
		encoding="ascii")
	(tmp_path / "main.cpp").write_text(
		CPP_PROBE.replace("CASES", _cases_c()), encoding="ascii")
	given = _run([HOST_CXX or "g++", "-std=c++17", *SANITIZE,
	              f"-I{ROOT / 'runtime' / 'c'}", f"-I{ROOT / 'runtime' / 'cpp'}",
	              f"-I{tmp_path}", str(tmp_path / "main.cpp"),
	              str(ROOT / "runtime" / "c" / "situ.c"),
	              "-o", str(tmp_path / "bin")], tmp_path / "bin")
	assert given == _expected(), given


def test_python_stays_in_the_frame(tmp_path: Path) -> None:
	schema, resolved = _built()
	(tmp_path / "situ_runtime.py").write_text(
		(ROOT / "runtime" / "python" / "situ_runtime.py").read_text(
			encoding="ascii"), encoding="ascii")
	path = tmp_path / "placed_unit.py"
	path.write_text(generate_py(schema, resolved, "unit").module,	# type: ignore[arg-type]
	                encoding="ascii")
	sys.path.insert(0, str(tmp_path))
	saved = sys.modules.pop("situ_runtime", None)
	try:
		spec = importlib.util.spec_from_file_location("placed_unit", path)
		assert spec is not None and spec.loader is not None
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		runtime = sys.modules["situ_runtime"]
	finally:
		sys.path.remove(str(tmp_path))
		sys.modules.pop("situ_runtime", None)
		if saved is not None:
			sys.modules["situ_runtime"] = saved

	for message, wrote, read, after in CASES:
		buffer = bytearray.fromhex(message)
		view = module.varv.at(runtime.Message(buffer), 0, len(buffer))
		if wrote == "version":
			with pytest.raises(runtime.VersionError):
				view.x
			with pytest.raises(runtime.VersionError):
				view.x = 0x1234
		else:
			assert f"ok {view.x}" == read, message
			view.x = 0x1234
		assert buffer.hex() == after, message


def test_rust_asks_the_version_before_storing(tmp_path: Path) -> None:
	schema, resolved = _built()
	module = generate_rs(schema, resolved, "unit").module	# type: ignore[arg-type]
	setter = module[module.index("pub fn set_x("):]
	setter = setter[:setter.index("\n\t}\n")]
	assert "-> Result<()>" in setter, setter
	assert setter.index("Err(Error::Version)") < setter.index("write_be"), \
		setter
	# ...and it builds, warnings refused, in the four-way driver.
	(tmp_path / "unit.situ").write_text(SCHEMA, encoding="ascii")
	fourway.build(tmp_path, tmp_path / "unit.situ")
