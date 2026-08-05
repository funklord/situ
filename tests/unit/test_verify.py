"""`situc verify`: the schema as a specification (26.67).

The command exists for a project that cannot take situ's usual bargain --
where the callers hold owned structs rather than views, or the build may not
gain a code generator -- and can still take a smaller one: keep the
hand-written codec, and let CI fail when the bytes and the schema disagree.

What is worth testing is therefore not that it accepts good vectors. It is
that it *rejects* bad ones, for the right reason and with a line number, and
that it writes nothing while doing so.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from every_schema import ROOT
from situc.cli import main

ARP     = ROOT / "examples" / "arp" / "arp.situ"
VECTORS = ROOT / "examples" / "arp" / "arp.vectors"


def test_committed_vectors_conform(capsys: pytest.CaptureFixture[str]) -> None:
	assert main(["verify", str(ARP), str(VECTORS)]) == 0
	assert "vectors conform" in capsys.readouterr().out


def test_a_wrong_byte_is_refused(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""The case the whole command is for.

	`wire --check` and `map --check` hold a schema to its own committed
	contracts and never read a real byte, so neither notices a schema that
	disagreed with the implementation from the day it was written. One byte
	of the protocol type is changed here, which is what that disagreement
	looks like from outside.
	"""
	bad = tmp_path / "bad.vectors"
	bad.write_text(VECTORS.read_text(encoding="ascii").replace(
		"00 01 08 00 06 04 00 01", "00 01 09 00 06 04 00 01", 1),
		encoding="ascii")

	assert main(["verify", str(ARP), str(bad)]) == 1

	out = capsys.readouterr().out
	assert "does not conform" in out
	assert "must_eq 2048" in out, "the refusal should name the constraint"
	assert f"{bad}:" in out, "and the line in the vector file"


def test_a_short_vector_is_refused(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""Wrong length and wrong value fail differently, and should read
	differently: one is bytes the layout cannot cover, the other is bytes it
	covers and forbids."""
	short = tmp_path / "short.vectors"
	short.write_text("arp_packet truncated 00 01 08 00\n", encoding="ascii")

	assert main(["verify", str(ARP), str(short)]) == 1
	assert "BoundsError" in capsys.readouterr().out


def test_an_unknown_struct_is_refused(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	names = tmp_path / "names.vectors"
	names.write_text("no_such_struct case 00 01\n", encoding="ascii")

	assert main(["verify", str(ARP), str(names)]) == 1
	assert "no struct `no_such_struct`" in capsys.readouterr().out


def test_an_empty_corpus_is_not_a_pass(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""Nought out of nought conforming is not evidence of anything, and a
	green exit there would be a CI job that passes for having no input --
	which is the failure this whole command is meant to prevent one level
	down."""
	empty = tmp_path / "empty.vectors"
	empty.write_text("# nothing but a comment\n", encoding="ascii")

	assert main(["verify", str(ARP), str(empty)]) == 1
	assert "holds no vectors" in capsys.readouterr().out


def test_it_generates_nothing(tmp_path: Path,
		capsys: pytest.CaptureFixture[str]) -> None:
	"""The property that makes it adoptable.

	A project taking this mode has declined to put a code generator in its
	build; a command that quietly wrote a `.py` beside the schema would have
	given it one anyway.
	"""
	work = tmp_path / "work"
	work.mkdir()
	before = set(work.rglob("*"))

	vectors = work / "copy.vectors"
	vectors.write_text(VECTORS.read_text(encoding="ascii"), encoding="ascii")

	assert main(["verify", str(ARP), str(vectors)]) == 0
	capsys.readouterr()

	assert set(work.rglob("*")) == before | {vectors}


def test_every_example_with_vectors_conforms_to_its_schema(
		capsys: pytest.CaptureFixture[str]) -> None:
	"""And the corpora already in the tree are held to their schemas.

	These vectors were laid out by other implementations -- glibc's ARP
	definitions, an archive GNU cpio wrote, bytes a netlink socket handed
	back -- so this is the differential check of `tests/unit/oracles.py` in
	the form a checking-only adopter would use it.
	"""
	found = sorted(ROOT.glob("examples/*/*.vectors"))
	assert found, "no committed vectors to check"

	failed = []
	for vectors in found:
		schema = vectors.with_suffix(".situ")
		if not schema.exists():
			continue
		if main(["verify", str(schema), str(vectors)]) != 0:
			failed.append(vectors.parent.name)
		capsys.readouterr()

	assert not failed, f"committed vectors do not conform: {failed}"
