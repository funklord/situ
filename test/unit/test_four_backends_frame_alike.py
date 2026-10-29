"""The four backends' `required`, answer for answer, over every schema.

**The framing layer had no cross-backend witness at all.** 26.725 measured
that `situc/codegen/differ.py` -- the differential that compares all five
spellings of the accessors over the corpus, and the source the C++ fuzz
harness is built from -- mentions neither `required` nor `framed`. And the
two corpus prefix sweeps in `test_required_over_the_corpus.py` check each
backend against PROPERTIES rather than against the others: two backends
can both be monotone, both progress, and still disagree about the number.

That matters here more than elsewhere. Section 26 puts it in one sentence:
*"four backends that disagreed would mean a schema means four things."*
Seven of this function's records in two weeks are a quantity computed in
two places from two things, and nothing was asking whether the four places
agreed.

`test_a_counted_run_is_framed.py` holds a different claim -- that each
backend HAS a framing walk, structurally, over one hand-written shape.
This asks what they ANSWER, over the corpus, numerically.

**It found Rust saturating at the wrong width on its first run** (26.732):
for a hostile length the other three answer 4294967295, the `uint32_t`
maximum they saturate at deliberately (26.651), and Rust answered
8589934594 because `usize::saturating_add` saturates at `usize`. Five
structs, and on a 32-bit target Rust would have agreed -- so the
divergence depended on the pointer width.

## What it compares, and what it cannot

Every struct whose `required` takes bytes and nothing else, in all four
backends at once. A struct with a `[stream]` argument takes it on
`required` too (0050), and calling one would mean inventing a value for a
fact the schema says is negotiated.

Four fills and prefixes 0 to 32, which is shorter than the property
sweeps' 48 because this builds four artifacts per schema rather than one.
The fills are the same four and chosen for the same reasons: 0x00 reads
every count as zero, 0xFF makes each length as large as the type allows --
which is the one that found 26.732 -- 0x5A is neither, and 0x01 gives a
small count with short elements so a counted run's own arithmetic is
reached.

It compares answers, not allocations: the exact-size buffers and the
sanitizers belong to `test_required_over_the_corpus.py`, which is where a
missing bound is caught. Two backends agreeing wrongly is what this cannot
see, and the properties are what see it.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest						# noqa: E402

from situc import ast					# noqa: E402
from situc.codegen.c import generate as generate_c	# noqa: E402
from situc.codegen.cpp import generate as generate_cpp	# noqa: E402
from situc.codegen.python import generate as generate_py	# noqa: E402
from situc.codegen.rust import generate as generate_rs	# noqa: E402
from situc.codegen.rust.emit import _pascal as pascal	# noqa: E402
from situc.diagnostics import Source			# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.parser import parse				# noqa: E402
from situc.resolve import ResolvedSchema, resolve	# noqa: E402

from every_schema import SCHEMAS			# noqa: E402

RUNTIME  = ROOT / "runtime"
HOST_CC  = shutil.which("cc") or shutil.which("gcc")
HOST_CXX = shutil.which("g++") or shutil.which("clang++")
RUSTC    = shutil.which("rustc")

#: How far to sweep, and with what. Shorter than the property sweeps'
#: 48 because this builds three binaries per schema rather than one.
LONGEST = 32
FILLS = (0x00, 0xFF, 0x5A, 0x01)

#: C's one-line signature, C++'s two-line one. Both pin the whole
#: parameter list, so a struct taking a `[stream]` argument is left out.
C_SIGNATURE = re.compile(
	r"static inline situ_err_t situ_(\w+)_required\(const uint8_t \*data,"
	r"\s*uint32_t have, uint32_t \*need\)")
CPP_OPENS = "static ::situ::rt::err required(const std::uint8_t *data,"
CPP_CLOSES = "std::uint32_t have, std::uint32_t &need) noexcept"

#: schema -> (structs compared, sequences compared), filled in as it runs.
ALIKE_ASKED: dict[str, tuple[int, int]] = {}


def ids(paths: list[Path]) -> list[str]:
	return [path.name for path in paths]


def analysed(path: Path) -> tuple[ast.Schema, ResolvedSchema]:
	source = Source(str(path), path.read_text(encoding="ascii"))
	parsed = parse(source)
	return parsed, resolve(parsed, solve(parsed))


def cpp_classes(header: str) -> list[str]:
	"""The classes whose `required` takes only bytes, read back out of the
	artifact rather than derived from the schema a second time.

	BOTH lines of the signature: the argument lands on the second one, so
	matching the first alone lets a `[stream]` struct through and the
	function pointer will not convert.
	"""
	seen: list[str] = []
	current: str | None = None
	lines = header.splitlines()
	for index, line in enumerate(lines):
		found = re.match(r"class (\w+)\b", line)
		if found is not None:
			current = found.group(1)
		if CPP_OPENS not in line or current is None:
			continue
		if index + 1 < len(lines) and CPP_CLOSES in lines[index + 1]:
			seen.append(current)
	return seen


def _fills() -> str:
	return ", ".join(f"0x{fill:02X}u" for fill in FILLS)


def c_probe(names: list[str]) -> str:
	calls = "\n".join(
		f'\task("{name}", &situ_{name}_required);' for name in names)
	return f'''#include "unit.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const uint8_t FILL[] = {{ {_fills()} }};

typedef situ_err_t (*ask_t)(const uint8_t *, uint32_t, uint32_t *);

/* One line per struct and fill: the name, the fill's index, then one
 * token per prefix -- `*n` for a complete message of n bytes, `n` for a
 * truncated answer of n, `x` for anything else. */
static void ask(const char *who, ask_t f)
{{
	for (unsigned i = 0; i < sizeof FILL; i++) {{
		printf("%s %u", who, i);
		for (uint32_t n = 0; n <= {LONGEST}u; n++) {{
			const uint32_t size = n != 0u ? n : 1u;
			uint8_t       *p = malloc(size);
			uint32_t       need = 0;
			situ_err_t     got;

			if (p == NULL) {{
				exit(1);
			}}
			memset(p, FILL[i], size);
			got = f(p, n, &need);
			free(p);

			if (got == SITU_OK) {{
				printf(" *%u", need);
			}} else if (got == SITU_ERR_TRUNCATED) {{
				printf(" %u", need);
			}} else {{
				printf(" x");
			}}
		}}
		printf("\\n");
	}}
}}

int main(void)
{{
{calls}
	return 0;
}}
'''


def cpp_probe(names: list[str], namespace: str) -> str:
	calls = "\n".join(
		f'\task("{name}", &::{namespace}::{name}::required);'
		for name in names)
	return f'''#include "unit.hpp"
#include <cstdio>
#include <cstdlib>
#include <cstring>

static const std::uint8_t FILL[] = {{ {_fills()} }};

typedef ::situ::rt::err (*ask_t)(const std::uint8_t *, std::uint32_t,
                                 std::uint32_t &);

static void ask(const char *who, ask_t f)
{{
	for (unsigned i = 0; i < sizeof FILL; i++) {{
		std::printf("%s %u", who, i);
		for (std::uint32_t n = 0; n <= {LONGEST}u; n++) {{
			const std::uint32_t size = n != 0u ? n : 1u;
			auto *p = static_cast<std::uint8_t *>(std::malloc(size));
			std::uint32_t need = 0;

			if (p == nullptr) {{
				std::exit(1);
			}}
			std::memset(p, FILL[i], size);

			const ::situ::rt::err got = f(p, n, need);

			std::free(p);
			if (got == ::situ::rt::err::ok) {{
				std::printf(" *%u", need);
			}} else if (got == ::situ::rt::err::truncated) {{
				std::printf(" %u", need);
			}} else {{
				std::printf(" x");
			}}
		}}
		std::printf("\\n");
	}}
}}

int main()
{{
{calls}
	return 0;
}}
'''


def rust_probe(names: list[str]) -> str:
	calls = "\n".join(
		f'\task("{name}", |b| unit::{pascal(name)}::required(b));'
		for name in names)
	fills = ", ".join(f"0x{fill:02X}" for fill in FILLS)
	return f'''mod situ_rt;
mod unit;

const FILL: [u8; {len(FILLS)}] = [{fills}];

fn ask(who: &str, f: impl Fn(&[u8]) -> situ_rt::Framing) {{
	for (i, fill) in FILL.iter().enumerate() {{
		let mut line = format!("{{}} {{}}", who, i);

		for n in 0..={LONGEST}usize {{
			let buf = vec![*fill; n];

			match f(&buf) {{
				situ_rt::Framing::Complete(k) =>
					line.push_str(&format!(" *{{}}", k)),
				situ_rt::Framing::Need(k) =>
					line.push_str(&format!(" {{}}", k)),
			}}
		}}
		println!("{{}}", line);
	}}
}}

fn main() {{
{calls}
}}
'''


def read_rows(text: str) -> dict[tuple[str, str], list[str]]:
	rows: dict[tuple[str, str], list[str]] = {}
	for line in text.splitlines():
		parts = line.split()
		if len(parts) >= 3:
			rows[(parts[0], parts[1])] = parts[2:]
	return rows


def python_rows(module: object, names: list[str],
		) -> dict[tuple[str, str], list[str]]:
	"""The same sweep in process, since Python needs no compiler."""
	rows: dict[tuple[str, str], list[str]] = {}
	truncated = getattr(module, "TruncatedError")
	for name in names:
		view = getattr(module, name, None)
		if view is None or not hasattr(view, "required"):
			continue
		for index, fill in enumerate(FILLS):
			answers: list[str] = []
			for n in range(0, LONGEST + 1):
				try:
					answers.append(f"*{view.required(bytes([fill]) * n)}")
				except truncated as short:
					answers.append(str(short.needed))
				except Exception:			# noqa: BLE001
					answers.append("x")
			rows[(name, str(index))] = answers
	return rows


@pytest.mark.skipif(HOST_CC is None or HOST_CXX is None or RUSTC is None,
                    reason="needs a C, a C++ and a Rust compiler")
@pytest.mark.parametrize("schema", SCHEMAS, ids=ids(SCHEMAS))
def test_the_four_answer_the_same(schema: Path, tmp_path: Path) -> None:
	"""Every struct all four frame, every fill, every prefix."""
	parsed, resolved = analysed(schema)
	built_c   = generate_c(parsed, resolved, "unit")
	built_cpp = generate_cpp(parsed, resolved, "unit")

	shared = sorted(set(C_SIGNATURE.findall(built_c.header))
	                & set(cpp_classes(built_cpp.header)))
	if not shared:
		pytest.skip(f"{schema.name} frames nothing all four emit")

	(tmp_path / "unit.h").write_text(built_c.header, encoding="ascii")
	(tmp_path / "unit.c").write_text(built_c.source, encoding="ascii")
	(tmp_path / "unit.hpp").write_text(built_cpp.header, encoding="ascii")
	(tmp_path / "c.c").write_text(c_probe(shared), encoding="ascii")
	namespace = str(getattr(parsed, "namespace", None) or "situ")
	(tmp_path / "p.cpp").write_text(cpp_probe(shared, namespace),
	                                encoding="ascii")

	rust = tmp_path / "rs"
	rust.mkdir()
	(rust / "situ_rt.rs").write_text(
		(RUNTIME / "rust" / "situ_rt.rs").read_text(encoding="ascii")
		.replace("#![no_std]\n", ""), encoding="ascii")
	(rust / "unit.rs").write_text(
		generate_rs(parsed, resolved, "unit").module, encoding="ascii")
	(rust / "main.rs").write_text(rust_probe(shared), encoding="ascii")

	rows: dict[str, dict[tuple[str, str], list[str]]] = {}
	for label, command in (
			("C", [HOST_CC or "cc", "-std=c11", "-O1",
			       f"-I{RUNTIME / 'c'}", f"-I{tmp_path}",
			       str(tmp_path / "c.c"), str(tmp_path / "unit.c"),
			       str(RUNTIME / "c" / "situ.c"),
			       "-o", str(tmp_path / "cbin")]),
			("C++", [HOST_CXX or "g++", "-std=c++17", "-O1",
			         f"-I{RUNTIME / 'c'}", f"-I{RUNTIME / 'cpp'}",
			         f"-I{tmp_path}", str(tmp_path / "p.cpp"),
			         str(RUNTIME / "c" / "situ.c"),
			         "-o", str(tmp_path / "ppbin")]),
			("Rust", [RUSTC or "rustc", "--edition", "2021",
			          str(rust / "main.rs"),
			          "-o", str(tmp_path / "rbin")])):
		made = subprocess.run(command, capture_output=True, text=True,
		                      cwd=str(rust))
		assert made.returncode == 0, f"{label}: {made.stderr[-2500:]}"
		binary = {"C": "cbin", "C++": "ppbin", "Rust": "rbin"}[label]
		ran = subprocess.run([str(tmp_path / binary)],
		                     capture_output=True, text=True)
		assert ran.returncode == 0, f"{label}: {ran.stderr[-2000:]}"
		rows[label] = read_rows(ran.stdout)

	module_path = tmp_path / "unitpy.py"
	module_path.write_text(generate_py(parsed, resolved, "unit").module,
	                       encoding="ascii")
	spec = importlib.util.spec_from_file_location("unitpy", module_path)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.path.insert(0, str(RUNTIME / "python"))
	try:
		spec.loader.exec_module(module)
	finally:
		sys.path.remove(str(RUNTIME / "python"))
	rows["Python"] = python_rows(module, shared)

	keys = set(rows["C"])
	for label in ("C++", "Rust", "Python"):
		keys &= set(rows[label])
	assert keys, f"{schema.name}: no struct answered in all four"

	for key in sorted(keys):
		answers = {label: rows[label][key] for label in rows}
		first = answers["C"]
		if all(value == first for value in answers.values()):
			continue
		for n in range(len(first)):
			seen = {label: (value[n] if n < len(value) else "-")
			        for label, value in answers.items()}
			if len(set(seen.values())) > 1:
				raise AssertionError(
					f"{schema.name}: `{key[0]}` with fill "
					f"0x{FILLS[int(key[1])]:02X} at have={n} is answered "
					+ ", ".join(f"{label}={value}"
					            for label, value in sorted(seen.items()))
					+ " -- four backends that disagreed would mean a "
					  "schema means four things")
		raise AssertionError(f"{schema.name}: `{key[0]}` differs in length")

	ALIKE_ASKED[schema.name] = (len(shared), len(keys))


@pytest.mark.skipif(HOST_CC is None or HOST_CXX is None or RUSTC is None,
                    reason="needs a C, a C++ and a Rust compiler")
def test_the_comparison_asked_something() -> None:
	"""The non-vacuity companion.

	Every assertion above is inside a loop over the structs all four
	emit, so a signature pattern that stopped matching would make this
	file pass while comparing nothing -- which is how the first version
	of it ran: the C symbol carries a `situ_` prefix the C++ class does
	not, the intersection was empty, and it reported zero differences
	over zero sequences.

	Measured when written: 40 schemas of the 46 this repository builds
	contributed and 6 were skipped for framing nothing all four emit,
	giving 221 structs and 884 (struct, fill) sequences.
	"""
	if not ALIKE_ASKED:
		pytest.skip("the per-schema comparisons did not run")
	structs = sum(count for count, _ in ALIKE_ASKED.values())
	rows    = sum(count for _, count in ALIKE_ASKED.values())
	assert len(ALIKE_ASKED) >= 36, (
		f"only {len(ALIKE_ASKED)} schemas contributed, of "
		f"{len(SCHEMAS)} in the tree")
	assert structs >= 180, f"only {structs} structs were compared"
	assert rows >= 700, f"only {rows} (struct, fill) sequences were compared"
