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


BUILDER = r"""#include <stdio.h>
#include "req_build.h"
int main(void)
{
	/* A person "a" since 7, held and inline; then the inline one a byte
	 * longer than the card it is, which the writer asks the card about. */
	const uint8_t card[]   = { 1, 1, 'a', 0, 0, 0, 7 };
	const uint8_t longer[] = { 1, 1, 'a', 0, 0, 0, 7, 0 };
	uint8_t  out[64];
	uint32_t wrote = 0u;
	int      fail  = 0;
	if (pr_request_build(out, sizeof out, 5u, card, sizeof card, card,
	                     sizeof card, &wrote) != SITU_OK || wrote != 17u)
		fail |= 1;
	if (pr_request_build(out, sizeof out, 5u, card, sizeof card, longer,
	                     sizeof longer, &wrote) == SITU_OK)
		fail |= 2;
	printf("fail=%d\n", fail);
	return fail;
}
"""


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
def test_a_linked_struct_nested_by_extent_keeps_its_builder(
		tmp_path: Path) -> None:
	"""fuzznet (inbox 20261121T1820Z, 26.1130): `card inline_card` is framed
	by the card's own `_required`, which the linked unit emits under its
	prefix. The builder looked for it under this unit's, found none, and
	`request` had a builder plain and none linked -- with a notice saying
	so while `generate`, reading the linked header, disagreed."""
	_units(tmp_path)
	linked = _build(tmp_path, "req.situ", "pr", "linked2", "--linked",
	                "card.situ=pc")
	assert "no builder for `request`" not in linked.stderr, linked.stderr
	header = (tmp_path / "linked2" / "req_build.h").read_text(encoding="ascii")
	assert "pr_request_build(" in header
	assert "pc_card_required(" in header and "pr_card_required(" not in header
	(tmp_path / "b.c").write_text(BUILDER, encoding="ascii")
	compiled = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
		 "-Wconversion", "-Wsign-conversion", f"-I{RUNTIME}",
		 f"-I{tmp_path / 'linked2'}", f"-I{tmp_path / 'card'}",
		 str(tmp_path / "b.c"), str(tmp_path / "linked2" / "req.c"),
		 str(tmp_path / "card" / "card.c"), str(RUNTIME / "situ.c"),
		 "-o", str(tmp_path / "b")],
		capture_output=True, text=True)
	assert compiled.returncode == 0, compiled.stderr
	ran = subprocess.run([str(tmp_path / "b")], capture_output=True,
	                     text=True, timeout=30)
	assert ran.stdout.strip() == "fail=0", ran.stdout
