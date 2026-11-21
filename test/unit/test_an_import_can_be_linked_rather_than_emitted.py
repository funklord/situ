"""An imported unit can be linked rather than emitted again (`--linked`).

fuzznet (inbox 20261121T0400Z): its contact request imports the contact
card for `[holds = fzn_contact_card]`, and under the request's prefix the
generated unit carried the card's whole checker again -- 4412 bytes of
text beside the card's own 4420 in the same binary, and a 5500-line
header. `--linked card.situ=PREFIX` names the unit that already emits the
card: its header is included, its functions called under its own prefix,
and nothing of it is emitted here but what it does not offer -- a
struct's `_extent`, which a unit writes only where it walks one.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT     = Path(__file__).resolve().parents[2]
RUNTIME  = ROOT / "runtime" / "c"
COMPILER = shutil.which("cc") or shutil.which("gcc")

CARD = """target buffer;
endian big;

enum kind : u8 { person = 1, group = 2 }

struct card {
	kind  k;
	u8    name_len [max = 32];
	u8    name[name_len];
	u32   since [min = 1];
}
"""

REQUEST = """target buffer;
endian big;

import "card.situ";

struct request {
	u16  id;
	u8   card_len;
	u8   card_bytes[card_len] [holds = card];
	card inline_card;
}
"""

PROGRAM = r"""#include <stdio.h>
#include "req.h"
int main(void)
{
	/* id; a held card (person, "a", since 7); an inline one (group, "bc",
	 * since 9). */
	uint8_t b[] = { 0,5, 7, 1,1,'a',0,0,0,7,  2,2,'b','c',0,0,0,9 };
	situ_msg_t m; situ_view_t v, c;
	int fail = 0;
	situ_msg_init(&m, b, sizeof b);
	if (pr_request_view(&m, 0u, sizeof b, &v) != SITU_OK) fail |= 1;
	if (pr_request_validate(v) != SITU_OK) fail |= 2;
	if (pr_request_card_bytes_view(v, &c) != SITU_OK
	    || @CARD@_card_since_get(c) != 7u) fail |= 4;
	if (pr_request_inline_card_view(v, &c) != SITU_OK
	    || @CARD@_card_since_get(c) != 9u) fail |= 8;
	b[9] = 0;	/* the held card's `since` breaks its [min = 1] */
	if (pr_request_validate(v) != SITU_ERR_CONSTRAINT) fail |= 16;
	printf("fail=%d\n", fail);
	return fail;
}
"""


def _build(tmp_path: Path, schema: str, prefix: str, out: str,
		*extra: str) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		[sys.executable, "-m", "situc", "build", str(tmp_path / schema),
		 "--layer", "edit", "--prefix", prefix, *extra,
		 "--out", str(tmp_path / out)],
		cwd=ROOT, capture_output=True, text=True)


def _units(tmp_path: Path) -> None:
	(tmp_path / "card.situ").write_text(CARD, encoding="ascii")
	(tmp_path / "req.situ").write_text(REQUEST, encoding="ascii")
	for args in (("card.situ", "pc", "card"),
	             ("req.situ", "pr", "plain"),
	             ("req.situ", "pr", "linked", "--linked", "card.situ=pc")):
		done = _build(tmp_path, *args)
		assert done.returncode == 0, done.stderr


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
def test_a_linked_unit_reads_through_the_other_units_functions(
		tmp_path: Path) -> None:
	_units(tmp_path)
	for form, card in (("plain", "pr"), ("linked", "pc")):
		(tmp_path / f"{form}.c").write_text(PROGRAM.replace("@CARD@", card),
		                                    encoding="ascii")
		sources = [str(tmp_path / form / "req.c")]
		includes = [f"-I{tmp_path / form}"]
		if form == "linked":
			sources.append(str(tmp_path / "card" / "card.c"))
			includes.append(f"-I{tmp_path / 'card'}")
		compiled = subprocess.run(
			[COMPILER or "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
			 "-Wconversion", "-Wsign-conversion", f"-I{RUNTIME}", *includes,
			 str(tmp_path / f"{form}.c"), *sources, str(RUNTIME / "situ.c"),
			 "-o", str(tmp_path / form / "m")],
			capture_output=True, text=True)
		assert compiled.returncode == 0, (form, compiled.stderr)
		ran = subprocess.run([str(tmp_path / form / "m")],
		                     capture_output=True, text=True, timeout=30)
		assert ran.stdout.strip() == "fail=0", (form, ran.stdout)


def test_the_linked_unit_emits_none_of_the_card(tmp_path: Path) -> None:
	_units(tmp_path)
	plain  = (tmp_path / "plain" / "req.h").read_text(encoding="ascii")
	linked = (tmp_path / "linked" / "req.h").read_text(encoding="ascii")
	assert '#include "card.h"' in linked
	assert "pr_card_view" in plain and "pr_card_view" not in linked
	assert "pc_card_validate" in (tmp_path / "linked" / "req.c").read_text(
		encoding="ascii")
	# The one thing the card's unit does not offer, emitted here under this
	# unit's own prefix.
	assert "pc_card_extent" not in linked and "pr_card_extent" in linked
	assert "pr_kind" not in linked
	assert len(linked) < len(plain)
	for name in ("req_build.h", "req_edit.h"):
		text = (tmp_path / "linked" / name).read_text(encoding="ascii") \
			if (tmp_path / "linked" / name).exists() else ""
		assert "pr_card_" not in text, name


def test_linked_is_refused_beyond_c(tmp_path: Path) -> None:
	_units(tmp_path)
	done = _build(tmp_path, "req.situ", "pr", "rs", "--target", "rust",
	              "--linked", "card.situ=pc")
	assert done.returncode != 0 and "--linked" in done.stderr


def test_linked_takes_file_equals_prefix(tmp_path: Path) -> None:
	_units(tmp_path)
	done = _build(tmp_path, "req.situ", "pr", "x", "--linked", "card.situ")
	assert done.returncode != 0 and "FILE=PREFIX" in done.stderr
