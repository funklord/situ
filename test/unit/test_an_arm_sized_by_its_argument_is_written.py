"""A builder writes an arm whose extent is its argument (26.1012).

`case 1: a_data(size = rdlength) a;` with `a_data { parameter u16 size;
u8 data[size] [holds = ipv4_address]; }` -- DNS's RDATA, and wasm's
section bodies the same way. Every builder refused them: the arm's struct
states no extent of its own, so there is no `_required` to ask whether the
caller's bytes are one of it. But its extent IS its argument, a field the
caller passes, so the bytes are one exactly when they are that long; their
content is checked by the closing `validate`, which hands the arm the same
argument a reader does. C only: the other three do not yet build a member
taking arguments.

Held to RFC 1035 section 4.1.3 for an A record, and to the WebAssembly core
spec's section layout for a custom section (id 0, LEB128 size, a name).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT     = Path(__file__).resolve().parents[2]
COMPILER = shutil.which("cc") or shutil.which("gcc")


def _built(tmp_path: Path, stem: str) -> Path:
	out = tmp_path / stem
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build",
		 str(ROOT / "example" / stem / f"{stem}.situ"), "--target", "c",
		 "--layer", "edit", "--out", str(out)],
		cwd = ROOT, capture_output = True, text = True)
	assert done.returncode == 0, done.stderr
	return out


def _run(tmp_path: Path, out: Path, stem: str, body: str) -> None:
	(tmp_path / f"{stem}_drive.c").write_text(
		f'#include <string.h>\n#include "{stem}_build.h"\n\n'
		f"int main(void)\n{{\n{body}\treturn 0;\n}}\n", encoding = "ascii")
	runtime = ROOT / "runtime" / "c"
	built = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-pedantic-errors", "-Wall", "-Wextra",
		 "-Werror", "-Wconversion", "-Wsign-conversion", f"-I{runtime}",
		 f"-I{out}", str(tmp_path / f"{stem}_drive.c"), str(out / f"{stem}.c"),
		 str(runtime / "situ.c"), "-o", str(tmp_path / f"{stem}_drive")],
		capture_output = True, text = True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / f"{stem}_drive")],
	                     capture_output = True)
	assert ran.returncode == 0, f"check {ran.returncode} failed"


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_a_dns_a_record(tmp_path: Path) -> None:
	"""example.com IN A 93.184.216.34, TTL 300: the owner name, TYPE 1,
	CLASS 1, TTL, RDLENGTH 4 and the address."""
	_run(tmp_path, _built(tmp_path, "dnsname"), "dnsname", """
	static const uint8_t owner[13] = {
		7, 'e', 'x', 'a', 'm', 'p', 'l', 'e', 3, 'c', 'o', 'm', 0 };
	static const uint8_t address[4] = { 93, 184, 216, 34 };
	static const uint8_t want[27] = {
		7, 'e', 'x', 'a', 'm', 'p', 'l', 'e', 3, 'c', 'o', 'm', 0,
		0, 1, 0, 1, 0, 0, 1, 0x2c, 0, 4, 93, 184, 216, 34 };
	uint8_t  buf[64];
	uint32_t wrote = 0;

	if (situ_resource_build_a(buf, sizeof buf, owner, 13u, 1u, 300u, 4u,
	                          address, 4u, &wrote) != SITU_OK
	    || wrote != 27u || memcmp(buf, want, 27u) != 0) {
		return 1;
	}
	/* RDLENGTH saying 5 over four bytes of address: refused. */
	if (situ_resource_build_a(buf, sizeof buf, owner, 13u, 1u, 300u, 5u,
	                          address, 4u, &wrote) != SITU_ERR_CONSTRAINT) {
		return 2;
	}
	/* Three bytes are not an IPv4 address, and the message says so. */
	if (situ_resource_build_a(buf, sizeof buf, owner, 13u, 1u, 300u, 3u,
	                          address, 3u, &wrote) == SITU_OK) {
		return 3;
	}
""")


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_a_wasm_custom_section(tmp_path: Path) -> None:
	"""Section id 0, its size as LEB128, then the custom section: a name,
	`name`, and its bytes."""
	_run(tmp_path, _built(tmp_path, "wasm"), "wasm", """
	static const uint8_t custom[7] = { 4, 'n', 'a', 'm', 'e', 1, 2 };
	static const uint8_t want[9] = { 0, 7, 4, 'n', 'a', 'm', 'e', 1, 2 };
	uint8_t  buf[32];
	uint32_t wrote = 0;

	if (situ_section_build_custom(buf, sizeof buf, 7u, custom, 7u, &wrote)
	    != SITU_OK || wrote != 9u || memcmp(buf, want, 9u) != 0) {
		return 1;
	}
	if (situ_section_build_custom(buf, sizeof buf, 8u, custom, 7u, &wrote)
	    != SITU_ERR_CONSTRAINT) {
		return 2;
	}
""")
