"""A setter for a member that is both versioned and covered (26.765).

    struct plain [version = ver] {
        u8 ver;
        tag u8 mac[4] covers(part);
        authenticated part { u8 a; u8 b [since = 2]; }
    }

Each backend had two setters for `b` in mind and wrote one of them. C and
Python wrote the COVERED one, which marks `mac` stale and asks nothing
about the version, so C wrote `b` past a whole six-byte version-1 message.
C++ and Rust wrote the VERSIONED one, which refuses an older message and
marks nothing, so `transmittable()` went on calling a message whose tag no
longer matched ready to send. Each now does both.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

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
struct plain [version = ver] {
	u8 ver;
	tag u8 mac[4] covers(part);
	authenticated part { u8 a; u8 b [since = 2]; }
}
"""


def _built() -> tuple[object, object]:
	schema = parse_text(SCHEMA)
	return schema, resolve(schema, solve(schema))


C_PROBE = """#include "unit.h"
#include <stdio.h>
#include <stdlib.h>
static int set(uint8_t version, uint32_t length)
{
	uint8_t *p = calloc(length, 1);
	situ_msg_t m;
	situ_view_t view;
	int got;
	p[0] = version;
	situ_msg_init(&m, p, length);
	if (situ_plain_view(&m, 0, length, &view) != SITU_OK) {
		free(p);
		return -1;
	}
	got = (int)situ_plain_b_set(&m, view, 9);
	if (got == SITU_OK && situ_msg_transmittable(&m) == SITU_OK) {
		got = 99;	/* written, and the tag not marked stale */
	}
	free(p);
	return got;
}
int main(void)
{
	printf("%d %d %d\\n", set(1, 6) == SITU_ERR_VERSION,
	       set(2, 6) == SITU_ERR_BOUNDS, set(2, 7) == SITU_OK);
	return 0;
}
"""

CPP_PROBE = """#include "unit.hpp"
#include <cstdio>
#include <cstdlib>
static int set(std::uint8_t version, std::uint32_t length)
{
	auto *p = static_cast<std::uint8_t *>(std::calloc(length, 1));
	p[0] = version;
	::situ::rt::message m(p, length);
	::situ::plain view;
	if (::situ::plain::at(m, 0, length, view) != ::situ::rt::err::ok) {
		std::free(p);
		return -1;
	}
	int got = static_cast<int>(view.set_b(m, 9));
	if (got == 0 && m.transmittable() == ::situ::rt::err::ok) {
		got = 99;
	}
	std::free(p);
	return got;
}
int main()
{
	std::printf("%d %d %d\\n",
	            set(1, 6) == static_cast<int>(::situ::rt::err::version),
	            set(2, 6) == static_cast<int>(::situ::rt::err::bounds),
	            set(2, 7) == 0);
	return 0;
}
"""


@pytest.mark.skipif(HOST_CC is None, reason="no host C compiler")
def test_c_refuses_an_older_message_and_marks_the_tag(tmp_path: Path) -> None:
	schema, resolved = _built()
	built = generate_c(schema, resolved, "unit")	# type: ignore[arg-type]
	(tmp_path / "unit.h").write_text(built.header, encoding="ascii")
	(tmp_path / "unit.c").write_text(built.source, encoding="ascii")
	(tmp_path / "main.c").write_text(C_PROBE, encoding="ascii")
	made = subprocess.run(
		[HOST_CC or "cc", "-std=c11", *SANITIZE,
		 f"-I{ROOT / 'runtime' / 'c'}", f"-I{tmp_path}",
		 str(tmp_path / "main.c"), str(tmp_path / "unit.c"),
		 str(ROOT / "runtime" / "c" / "situ.c"), "-o", str(tmp_path / "bin")],
		capture_output=True, text=True)
	assert made.returncode == 0, made.stderr[-2000:]
	ran = subprocess.run([str(tmp_path / "bin")], capture_output=True,
	                     text=True)
	assert ran.returncode == 0, ran.stderr[-2000:]
	assert ran.stdout.strip() == "1 1 1", ran.stdout


@pytest.mark.skipif(HOST_CXX is None, reason="no host C++ compiler")
def test_cpp_refuses_an_older_message_and_marks_the_tag(tmp_path: Path) -> None:
	schema, resolved = _built()
	(tmp_path / "unit.hpp").write_text(
		generate_cpp(schema, resolved, "unit").header,	# type: ignore[arg-type]
		encoding="ascii")
	(tmp_path / "main.cpp").write_text(CPP_PROBE, encoding="ascii")
	made = subprocess.run(
		[HOST_CXX or "g++", "-std=c++17", *SANITIZE,
		 f"-I{ROOT / 'runtime' / 'c'}", f"-I{ROOT / 'runtime' / 'cpp'}",
		 f"-I{tmp_path}", str(tmp_path / "main.cpp"),
		 str(ROOT / "runtime" / "c" / "situ.c"), "-o", str(tmp_path / "bin")],
		capture_output=True, text=True)
	assert made.returncode == 0, made.stderr[-2000:]
	ran = subprocess.run([str(tmp_path / "bin")], capture_output=True,
	                     text=True)
	assert ran.returncode == 0, ran.stderr[-2000:]
	assert ran.stdout.strip() == "1 1 1", ran.stdout


def test_python_refuses_an_older_message(tmp_path: Path) -> None:
	schema, resolved = _built()
	(tmp_path / "situ_runtime.py").write_text(
		(ROOT / "runtime" / "python" / "situ_runtime.py").read_text(
			encoding="ascii"), encoding="ascii")
	path = tmp_path / "covered_unit.py"
	path.write_text(generate_py(schema, resolved, "unit").module,	# type: ignore[arg-type]
	                encoding="ascii")
	sys.path.insert(0, str(tmp_path))
	saved = sys.modules.pop("situ_runtime", None)
	try:
		spec = importlib.util.spec_from_file_location("covered_unit", path)
		assert spec is not None and spec.loader is not None
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		runtime = sys.modules["situ_runtime"]
	finally:
		sys.path.remove(str(tmp_path))
		sys.modules.pop("situ_runtime", None)
		if saved is not None:
			sys.modules["situ_runtime"] = saved

	old = runtime.Message(bytearray([1, 0, 0, 0, 0, 5]))
	view = module.plain.at(old, 0, 6)
	with pytest.raises(runtime.VersionError):
		view.set_b(old, 9)
	new = runtime.Message(bytearray([2, 0, 0, 0, 0, 5, 6]))
	view = module.plain.at(new, 0, 7)
	view.set_b(new, 9)
	assert bytes(new.buffer)[6] == 9


def test_rust_marks_the_tag() -> None:
	schema, resolved = _built()
	module = generate_rs(schema, resolved, "unit").module	# type: ignore[arg-type]
	setter = module[module.index("pub fn set_b("):]
	setter = setter[:setter.index("\n\t}\n")]
	assert "dirty: &mut Dirty" in setter and "dirty.mark(" in setter, setter
	assert "Err(Error::Version)" in setter, setter
