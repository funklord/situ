"""A relation may name a `const` and an enum member (26.984).

`must response.k == kind.ack` was "`kind` is not a message this relation
was given": every name in a relation was read as a path into one of its two
messages. A `when`, a bound and a size all fold both. The parser folds them
before anything reads a relation, so the four backends, both walkers and the
dissector take numbers.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from situc.diagnostics import SituError
from situc.layout import solve
from situc.pack import pack
from situc.parser import parse_text
from situc.resolve import resolve
from test_the_c_walker_answers_a_relation import COMPILER, DRIVER, WALKER
from walker import report
from walker.image import load
from walker.walk import acquire

ROOT = Path(__file__).resolve().parents[2]

SCHEMA = """target buffer;
endian big;
const STEP = 1;
enum kind : u8 { ask = 1, ack = 2 }
struct msg {
	kind k;
	u8 seq;
}
relation reply(request: msg, response: msg) {
	must response.k == kind.ack && response.seq == request.seq + STEP;
}
"""

#: Request, response, and whether the pair holds.
PAIRS = [
	(bytes([1, 7]), bytes([2, 8]), True),
	(bytes([1, 7]), bytes([1, 8]), False),		# not an ack
	(bytes([1, 7]), bytes([2, 9]), False),		# not the next
	(bytes([1, 7]), bytes([2, 7]), False),
]


def test_a_parameter_shadows_a_constant_of_its_name() -> None:
	"""`kind.k` is a path where `kind` is a parameter, not an enum member."""
	text = SCHEMA.replace("relation reply(request: msg, response: msg)",
	                      "relation reply(request: msg, kind: msg)")
	text = text.replace("must response.k == kind.ack && "
	                    "response.seq == request.seq + STEP;",
	                    "must kind.seq == request.seq + STEP;")
	from situc.relation import paths_in
	schema = parse_text(text, path = "r.situ")
	relation = schema.relations()[0]
	assert paths_in(relation.body[0].expr) == ["kind.seq", "request.seq"]
	resolve(schema, solve(schema))


@pytest.mark.parametrize("target", ["c", "cpp", "rust", "python"])
def test_all_four_emit_it(tmp_path: Path, target: str) -> None:
	source = tmp_path / "r.situ"
	source.write_text(SCHEMA, encoding = "ascii")
	built = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(source), "--target",
		 target, "--layer", "relate", "--refuse-ungenerated", "--out",
		 str(tmp_path / "out")], capture_output = True, text = True,
		cwd = ROOT)
	assert built.returncode == 0, built.stderr


def test_the_python_backend_holds_a_reply_to_it(tmp_path: Path) -> None:
	source = tmp_path / "r.situ"
	source.write_text(SCHEMA, encoding = "ascii")
	subprocess.run([sys.executable, "-m", "situc", "build", str(source),
	                "--target", "python", "--layer", "relate", "--out",
	                str(tmp_path)], check = True, capture_output = True,
	               cwd = ROOT)
	sys.path[:0] = [str(tmp_path), str(ROOT / "runtime" / "python")]
	try:
		views  = importlib.import_module("r")
		relate = importlib.import_module("r_relate")
		for request, response, holds in PAIRS:
			try:
				relate.rel_reply(views.msg.at(views.Message(request)),
				               views.msg.at(views.Message(response)))
				said = True
			except views.ConstraintError:
				said = False
			assert said == holds, (request, response)
	finally:
		sys.path.remove(str(tmp_path))
		sys.path.remove(str(ROOT / "runtime" / "python"))
		for name in ("r", "r_relate"):
			sys.modules.pop(name, None)


@pytest.mark.skipif(COMPILER is None, reason = "no C compiler")
def test_both_walkers_hold_a_reply_to_it(tmp_path: Path) -> None:
	schema = parse_text(SCHEMA, path = "r.situ")
	blob, _ = pack(schema, resolve(schema, solve(schema)), metadata = True)
	(tmp_path / "img").write_bytes(blob)
	(tmp_path / "drive.c").write_text(DRIVER, encoding = "ascii")
	built = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
		 "-Wconversion", "-Wsign-conversion", f"-I{WALKER}",
		 str(tmp_path / "drive.c"), str(WALKER / "situ_walk.c"),
		 "-o", str(tmp_path / "drive")], capture_output = True, text = True)
	assert built.returncode == 0, built.stderr[-2000:]
	image = load(blob)
	shape = image.relations[0].request
	for request, response, holds in PAIRS:
		walked = report.relate(image, 0, acquire(image, request, shape),
		                       acquire(image, response, shape))
		ran = subprocess.run([str(tmp_path / "drive"), str(tmp_path / "img"),
		                      request.hex(), response.hex()],
		                     capture_output = True, text = True)
		assert str(walked) == ("0" if holds else "2"), (request, response)
		assert ran.stdout.strip() == str(walked), (request, response)


def test_a_constant_alone_is_still_one_message() -> None:
	"""Folding does not make `must b.k == kind.ack` a relation: it reads one
	message, which is a `when`."""
	text = SCHEMA.replace("response.k == kind.ack && "
	                      "response.seq == request.seq + STEP",
	                      "response.k == kind.ack")
	with pytest.raises(SituError) as caught:
		parse_text(text, path = "r.situ")
	assert "reads one message" in caught.value.diagnostic.render()
