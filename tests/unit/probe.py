"""Run one composed schema through every oracle there is, and say what happened.

Three answers are correct and one is not:

  * **refused** -- `situc` reported a diagnostic. Most of the composition space
    is illegal and a refusal is the right answer to all of it (17.0).
  * **empty** -- nothing a driver can acquire, which a schema of declarations
    can be.
  * **agreed** -- four backends compiled and said the same thing about every
    buffer.

Anything else is a defect, and which kind is worth keeping apart: a traceback
out of the compiler is not the same failure as generated C that will not build,
and neither is the same as four backends that build and disagree.

The dissector is asked too where Lua is here. It is the artifact with no
compiler behind it, which is where invariant 61 says the defects live -- and
the last three folds bear that out.
"""

from __future__ import annotations

import random
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import fourway
from compose import Case
from every_schema import ROOT
from situc.cli import analyse
from situc.diagnostics import SituError
from situc.dissector import generate as generate_dissector
from situc.parser import parse

LUA     = shutil.which("lua5.4") or shutil.which("lua")
HARNESS = ROOT / "tests" / "lua" / "dissect.lua"

#: Buffers per case. Fewer than the corpus sweep uses, because there are far
#: more cases: what a composed schema is for is reaching the *shape*, and the
#: shapes that need many buffers to reach are in the corpus already.
BUFFERS = 16


@dataclass(frozen=True)
class Outcome:
	"""What one case did. `kind` is the headline; `detail` is for a reader."""

	case:   Case
	kind:   str		# refused | empty | agreed | crash | build | disagree
	detail: str = ""

	@property
	def ok(self) -> bool:
		return self.kind in ("refused", "empty", "agreed")


def run(case: Case, tmp: Path, seed: int = 20260804) -> Outcome:
	"""One case, through the compiler and then through four backends."""
	path = tmp / "unit.situ"
	path.write_text(case.schema(), encoding="ascii")

	# The first oracle, and the cheapest: `situc` may refuse this, and must
	# not fall over. A `SituError` carries a blame chain and a span; anything
	# else is the compiler crashing on input it never rejected.
	try:
		source, resolved, _ = analyse(path)
	except SituError as refusal:
		return Outcome(case, "refused", str(refusal.diagnostic.message))
	except Exception as broken:			# noqa: BLE001
		return Outcome(case, "crash", f"{type(broken).__name__}: {broken}")

	try:
		command = fourway.build(tmp, path)
	except fourway.BuildFailed as failed:
		return Outcome(case, "build", str(failed))
	except Exception as broken:			# noqa: BLE001
		return Outcome(case, "crash", f"{type(broken).__name__}: {broken}")

	if not command:
		return Outcome(case, "empty")

	rng = random.Random(seed)
	for _ in range(BUFFERS):
		packet = fourway.draw(rng)
		try:
			given = {name: fourway.answers(argv, packet, tmp)
			         for name, argv in command.items()}
		except fourway.BuildFailed as died:
			return Outcome(case, "build",
			               f"{packet.hex()}\n{died}")

		if len(set(given.values())) != 1:
			return Outcome(case, "disagree", _diff(packet, given))

	if LUA is not None:
		# A different name: `died` above is the exception the `except` clause
		# binds, and rebinding it is the one thing a caught name may not have
		# done to it.
		refused = _dissects(source, resolved, tmp, seed)
		if refused is not None:
			return Outcome(case, "build", refused)

	return Outcome(case, "agreed")


def _diff(packet: bytes, given: dict[str, str]) -> str:
	"""The disagreement, with the first differing line called out.

	Four whole listings is what this used to print, and finding the one line
	that differs in them is the reader's job done by hand every time.
	"""
	lines: list[str] = [f"buffer {packet.hex()}"]
	first = given["c"].splitlines()
	for name, text in sorted(given.items()):
		if name == "c":
			continue
		other = text.splitlines()
		for left, right in zip(first, other):
			if left != right:
				lines.extend([f"  c    : {left}", f"  {name:5}: {right}"])
				break
		if len(first) != len(other):
			lines.append(f"  c has {len(first)} lines, {name} has {len(other)}")
	return "\n".join(lines)


def _dissects(source: object, resolved: object, tmp: Path,
		seed: int) -> str | None:
	"""Whether the Lua dissector survives its own schema's bytes.

	A dissector cannot disagree with the other four -- nothing compares what
	Wireshark shows against what an accessor returns yet -- so what is asked
	here is only that it runs. That is a low bar and it has caught a negative
	length and an arithmetic-on-nil already.
	"""
	lua = tmp / "unit.lua"
	try:
		lua.write_text(
			generate_dissector(parse(source), resolved, "unit"),  # type: ignore[arg-type]
			encoding="ascii")
	except Exception as broken:			# noqa: BLE001
		return f"dissector: {type(broken).__name__}: {broken}"

	structs = getattr(resolved, "structs", {})
	rng     = random.Random(seed)
	for name, struct in sorted(structs.items()):
		if struct.layout.register is not None:
			continue
		for _ in range(4):
			packet = fourway.draw(rng)
			assert LUA is not None
			result = subprocess.run(
				[LUA, str(HARNESS), str(lua), name, packet.hex()],
				capture_output=True, text=True)
			if result.returncode != 0:
				return (f"dissector/{name} died on {len(packet)} bytes:\n"
				        f"  {packet.hex()}\n{result.stderr}")
	return None
