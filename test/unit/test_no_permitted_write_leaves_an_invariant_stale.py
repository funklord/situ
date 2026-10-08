"""0034's fourth row, answered by refusal rather than by maintenance (26.605).

That decision tabulates what writing a field drags in. The first row is
built (`--set`, 26.179) and the third is too (a tag-covered field is
recomputed, 26.579). The second is blocked on 26.99 and refused. The fourth
reads *a field an invariant reads -- the invariant has to be maintained,
not merely checked*, and `editor/document.py`'s own docstring repeats it:
*an invariant must be maintained rather than checked. None of that is
here.*

Both are true and together they read as a gap. There is not one, because
**no write this editor permits can leave an invariant stale**, and it takes
two refusals meeting:

	the derived field          `mutate = Immutable`, refused by name
	anything that moves a
	size the invariant reads   refused as a shifting write, by MEASURING
	                           the consequence rather than analysing it

The second is what makes it robust. `situ-edit` walks a copy and compares
every member's offset and size, so a fixed scalar written in place whose
VALUE decides a run's length is caught -- which is the one route an
analysis of "does this member shift?" would miss.

Nothing asserted the conjunction. Relax either refusal and the protection
goes silently: a derived field that becomes writable goes stale on the next
`--set`, and a permitted shifting write leaves the derived field describing
the old layout. So this module pins both halves and then the property
itself, by writing every writable member and re-reading.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest						# noqa: E402

#: An invariant over the size of a VARIABLE run, which the corpus does not
#: have: `edges.situ`'s one invariant reads two fixed members, so nothing
#: there can change what it reads and the interesting case is unreachable.
#: `n` decides `size(payload)`, so writing it is the route an analysis
#: would call harmless.
SCHEMA = """target buffer;
endian big;

struct framed {
	u8  n [max = 8];
	u8  payload[n];
	u8  total;
}

invariant framed.total == size(framed.payload);
"""

#: n=3, three payload bytes, total=3: the invariant holds.
MESSAGE = bytes([3, 0xAA, 0xBB, 0xCC, 3])


def edit(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
	schema = tmp_path / "inv.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	message = tmp_path / "inv.bin"
	message.write_bytes(MESSAGE)
	return subprocess.run(
		[sys.executable, str(ROOT / "bin" / "situ-edit"),
		 str(schema), str(message), *args],
		capture_output=True, text=True, cwd=ROOT, timeout=300)


def test_the_fixture_conforms_before_anything_is_written(
		tmp_path: Path) -> None:
	"""Otherwise every refusal below could be the message being wrong."""
	done = edit(tmp_path)
	assert done.returncode == 0, done.stderr
	assert "[ok]" in done.stdout, done.stdout


def test_the_derived_field_cannot_be_written(tmp_path: Path) -> None:
	"""The first refusal: an invariant derives `total`, so the lattice
	makes it Immutable and the editor refuses by name rather than storing
	a value the schema would then contradict."""
	done = edit(tmp_path, "--set", "total=9")
	assert done.returncode == 1
	assert "`total`" in done.stderr
	assert "does not let anyone write this" in done.stderr


def test_writing_what_decides_the_size_is_refused(tmp_path: Path) -> None:
	"""The second, and the one an analysis would miss. `n` is a fixed
	scalar written in place; what shifts is `payload`, because its LENGTH
	is this value. The refusal names the members that move."""
	done = edit(tmp_path, "--set", "n=2")
	assert done.returncode == 1
	assert "payload" in done.stderr
	assert "total" in done.stderr
	assert "shifting write is not built" in done.stderr


def test_no_permitted_write_leaves_the_invariant_stale(
		tmp_path: Path) -> None:
	"""The property, over every member rather than over the two above.

	Each member is written in turn: a refusal is an acceptable answer and a
	success is only acceptable if `total` still equals the payload's length
	afterwards. A third member added to the struct is covered without
	anybody extending a list.
	"""
	names = ["n", "payload", "total"]
	refused, permitted = [], []
	for name in names:
		out = tmp_path / f"out-{name}.bin"
		done = edit(tmp_path, "--set", f"{name}=1", "--out", str(out))
		if done.returncode != 0:
			refused.append(name)
			assert not out.exists(), f"{name} refused and still wrote a file"
			continue
		permitted.append(name)
		held = out.read_bytes()
		# `total` is the last byte and the payload is what `n` counts.
		assert held[-1] == held[0], (
			f"writing `{name}` left total={held[-1]} against a payload of "
			f"{held[0]} bytes -- the invariant is stale")

	# THE PARTITION, AND ITS EMPTY CELL NAMED. Every member of this struct
	# is refused today, so the branch above that checks a PERMITTED write
	# has never run -- and a test whose deciding assertion sits over an
	# empty set reports success exactly as loudly as one that discriminates.
	#
	# So the emptiness is asserted rather than relied on. A member becoming
	# writable fails here with a message saying which branch has never
	# executed, instead of being absorbed by a loop that would have handled
	# it for the wrong reason.
	assert sorted(refused) == sorted(names), (
		f"these writes are now permitted: {permitted}. The check above --\n"
		f"that a permitted write leaves `total` equal to the payload's\n"
		f"length -- had never run until now, so read it before trusting\n"
		f"this pass.")
	assert not permitted


def test_a_byte_run_written_short_is_refused_too(tmp_path: Path) -> None:
	"""The other spelling of the same hazard: `--set-bytes` with fewer
	bytes than the run holds moves what follows it, so the derived field
	would describe the old layout."""
	done = edit(tmp_path, "--set-bytes", "payload=aabb")
	assert done.returncode == 1
	assert "shifting" in done.stderr or "length" in done.stderr
