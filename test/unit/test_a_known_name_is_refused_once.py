"""A `known` name is a method on the view, so it cannot be one already there.

Rust and Python give each `known` name an accessor, and C and C++ do not. A
name equal to a member's, or to one generated for it, was therefore four
answers to one schema (26.911). `VIEW_SURFACE` and the affix tables are a list
somebody wrote, so the sweep here asks the generated code instead: every name
it binds has to be refused as a `known` name.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from every_schema import ROOT
from situc.cli import analyse
from situc.codegen.python import generate as generate_python
from situc.codegen.rust import generate as generate_rust
from situc.diagnostics import SituError
from situc.parser import parse

sys.path.insert(0, str(ROOT / "runtime" / "python"))
from situ_runtime import View  # type: ignore[import-not-found]  # noqa: E402

SHAPE = """
endian big;
struct m {{
	u8 kind;
	u8 n;
	u16 port;
	u8 body[n];
	variant which switch (kind) {{
		case 1: u8 one;
		default: u16 other_arm;
	}}
	tlv items (
		tag_type    = u8,
		length_type = u8,
		known       = {{ {known} }},
		unknown     = skip,
	);
}}
"""


def known(*names: str) -> str:
	"""The `known` map giving each name a tag of its own."""
	return ", ".join(f"{tag} : {{ name = {name}, type = u8 }}"
	                 for tag, name in enumerate(names, 1))


def refused(source: str, tmp_path: Path) -> bool:
	"""Whether THIS rule refused the schema.

	Only this rule: any other refusal raises, since a shape that does not
	parse would otherwise pass every test here.
	"""
	path = tmp_path / "m.situ"
	path.write_text(source, encoding = "ascii")
	try:
		analyse(path)
	except SituError as refusal:
		if "known tag" in refusal.diagnostic.message:
			return True
		raise
	return False


def bound(tmp_path: Path) -> set[str]:
	"""Every method name the two backends give the view."""
	path = tmp_path / "m.situ"
	path.write_text(SHAPE.format(known = known("plain")), encoding = "ascii")
	source, resolved = analyse(path)[:2]
	rust = generate_rust(parse(source), resolved, "m").module
	python = generate_python(parse(source), resolved, "m").module
	return (set(re.findall(r"pub (?:const )?fn ([a-z][a-z_0-9]*)", rust))
	        | set(re.findall(r"^\tdef ([a-z][a-z_0-9]*)", python, re.M))
	        | {name for name in dir(View) if not name.startswith("_")}) \
	       - {"plain"}


@pytest.mark.parametrize("name", [
	"kind", "items", "items_find", "items_count", "body_len", "set_port",
	"validate", "bytes", "as_bytes", "at", "new",
])
def test_a_taken_name_is_refused(name: str, tmp_path: Path) -> None:
	"""The schema line says so, whichever backend was asked for."""
	assert refused(SHAPE.format(known = known(name)), tmp_path)


TWO_ARMS = """
endian big;
struct m {{
	u8 kind;
	variant which switch (kind) {{
		case 1: tlv first (tag_type = u8, length_type = u8,
		                   known = {{ 1 : {{ name = mtu, type = u8 }} }},
		                   unknown = skip);
		default: tlv second (tag_type = u8, length_type = u8,
		                     known = {{ 1 : {{ name = {name}, type = u8 }} }},
		                     unknown = skip);
	}}
}}
"""


def test_two_regions_share_the_views_names(tmp_path: Path) -> None:
	"""Rust binds both on one view, so one name is two definitions."""
	assert refused(TWO_ARMS.format(name = "mtu"), tmp_path)
	assert not refused(TWO_ARMS.format(name = "hop_limit"), tmp_path)


def test_a_free_name_stays_free(tmp_path: Path) -> None:
	"""The positive control: an ordinary name is not caught by the rule."""
	assert not refused(SHAPE.format(known = known("mtu", "hop_limit")),
	                   tmp_path)


def test_every_generated_name_is_refused(tmp_path: Path) -> None:
	"""The list is held to the generated code, which 0062 says goes short.

	Harvested from what the two backends emit for a shape with a run, an arm
	and a region, and from `View`. A name the emitters gain and the rule does
	not know fails here. Forced past the rule such a name is rustc's E0592, or
	Python's assertion, or a quiet replacement of what `View` holds.
	"""
	names = sorted(bound(tmp_path))
	assert len(names) > 20, names
	assert not [name for name in names
	            if not refused(SHAPE.format(known = known(name)), tmp_path)]
