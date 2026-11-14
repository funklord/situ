"""A run ending at any of several delimiters is written with the first
(26.1021).

HTTP's `u8 value[] until "\\r\\n" | '\\n' [trim]` was refused by every
builder: "which byte to write after it is not stated". RFC 9112 section 2.2
states it -- a sender MUST generate CRLF, and a bare LF is what a recipient
MAY also accept -- and the schema lists CRLF first. So the writer puts the
first down. Where the reader then stops depends on the content too, so the
whole message is asked afterwards, as for a span its expression sizes
(26.1016): a value that would read back shorter is refused.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from test_an_arm_sized_by_its_argument_is_written import (COMPILER, ROOT,
                                                          _built, _run)

#: (value, what comes back): the bytes, or `constraint`.
CASES = [(b"example.com", "486f73743a6578616d706c652e636f6d0d0a"),
         (b"a\nb", "constraint"),		# a bare LF ends it early
         (b"a\r\nb", "constraint")]		# so does a CRLF


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_c(tmp_path: Path) -> None:
	calls = ""
	for i, (value, want) in enumerate(CASES):
		spelt = ", ".join(str(b) for b in value)
		if want == "constraint":
			test = f"if (e != SITU_ERR_CONSTRAINT) return {i + 1};"
		else:
			n    = len(want) // 2
			test = (f"if (e != SITU_OK || wrote != {n}u"
			        f" || memcmp(buf, want{i}, {n}u) != 0) return {i + 1};")
		calls += f"""
	{{
		static const uint8_t value[] = {{ {spelt} }};
		situ_err_t e = situ_header_field_build(buf, sizeof buf, name, 4u,
		                                       value, {len(value)}u, &wrote);
		{test}
	}}"""
	wants = "".join(
		f"\tstatic const uint8_t want{i}[] = {{ "
		+ ", ".join(f"0x{want[j:j + 2]}" for j in range(0, len(want), 2))
		+ " };\n"
		for i, (_, want) in enumerate(CASES) if want != "constraint")
	_run(tmp_path, _built(tmp_path, "http"), "http", f"""
	static const uint8_t name[4] = {{ 'H', 'o', 's', 't' }};
{wants}	uint8_t  buf[64];
	uint32_t wrote = 0;
{calls}
""")


def test_python(tmp_path: Path) -> None:
	out = tmp_path / "py"
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build",
		 str(ROOT / "example" / "http" / "http.situ"), "--target", "python",
		 "--layer", "edit", "--out", str(out)],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	(out / "situ_runtime.py").write_text(
		(ROOT / "runtime" / "python" / "situ_runtime.py").read_text(
			encoding = "ascii"), encoding = "ascii")
	(out / "run.py").write_text(f"""import sys
sys.path.insert(0, ".")
import http_build
from situ_runtime import ConstraintError
for value in {[value for value, _ in CASES]!r}:
	buf = bytearray(64)
	try:
		n = http_build.build_header_field(buf, b"Host", value)
		print(bytes(buf[:n]).hex())
	except ConstraintError:
		print("constraint")
""", encoding = "ascii")
	ran = subprocess.run([sys.executable, "-I", "run.py"], cwd = out,
	                     capture_output = True, text = True)
	assert ran.returncode == 0, ran.stderr
	assert ran.stdout.split() == [want for _, want in CASES], ran.stdout


def test_several_with_an_escape_stay_refused(tmp_path: Path) -> None:
	"""Which delimiter an escape protects is not stated, so that shape
	keeps its refusal -- reached here because no corpus schema has it."""
	schema = tmp_path / "esc.situ"
	schema.write_text('target buffer;\nstruct line {\n'
	                  '\tu8 text[] until "\\r\\n" | "\\n" '
	                  '[escape = "\\\\"];\n}\n', encoding = "ascii")
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(schema), "--target",
		 "c", "--layer", "edit", "--out", str(tmp_path / "out")],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	assert "no builder for `line`: `text` ends at any of 2 delimiters and " \
	       "escapes" in done.stderr, done.stderr
