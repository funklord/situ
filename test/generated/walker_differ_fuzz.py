"""The Python walker against the generated Python reader, coverage-guided
over the WALK (26.1142).

    python3 walker_differ_fuzz.py SCHEMA WORKDIR [libFuzzer flags...]

`make fuzz-walker-differ` runs it once per corpus schema. Each input is
handed to the generated Python reader's differ driver -- the four-way
differential's Python side, run in this process -- and to the walker's
`report.listing`; where both print a line about the same member of the
same struct, the lines must be equal, and every sum the driver answers the
walk must answer too. That is `test_walker`'s own comparison, made a fuzz
target: the corpora fuzzed before were guided by the generated code, so a
path through the walk that no generated reader takes was never driven.
Its first run, 60 s a schema, found four disagreements, each a walker
fault (26.1142).

atheris instruments `walker/` and nothing else, imported before anything
that would import it uninstrumented. A difference raises, which atheris
reports with the input saved; libFuzzer's `-max_total_time` bounds a run.

One divergence is accepted and skipped (26.1142's record): a run's count
where the walk and the reader both refuse the struct, since the run's last
element is then one cut short at the frame's end and a count in a refused
message is not a contract either side owes.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Imported by name so mypy, which has no stubs for it, does not need any.
atheris = importlib.import_module("atheris")

with atheris.instrument_imports(include=["walker"]):
	from walker import report				# noqa: E402
	from walker.image import load				# noqa: E402

from situc import pack as packer				# noqa: E402
from situc.codegen import differ				# noqa: E402
from situc.codegen.python import generate as generate_py	# noqa: E402
from situc.diagnostics import Source				# noqa: E402
from situc.layout import solve					# noqa: E402
from situc.parser import parse					# noqa: E402
from situc.resolve import resolve				# noqa: E402

SCHEMA = Path(sys.argv[1])
WORK = Path(sys.argv[2])


def by_member(text: str) -> dict[tuple[str, str], str]:
	"""A listing as (section, member) -> line, `test_walker._by_member`."""
	found: dict[tuple[str, str], str] = {}
	where = ""
	for line in text.split("\n"):
		if line.startswith("-- "):
			where = line[3:]
			continue
		if not line or " " not in line:
			continue
		found[(where, line.split(" ", 1)[0])] = line
	return found


parsed = parse(Source(str(SCHEMA), SCHEMA.read_text(encoding="utf-8")))
resolved = resolve(parsed, solve(parsed))
blob, _ = packer.pack(parsed, resolved, metadata=True)
image = load(blob)
WORK.mkdir(parents=True, exist_ok=True)
(WORK / "situ_runtime.py").write_text(
	(ROOT / "runtime" / "python" / "situ_runtime.py")
	.read_text(encoding="ascii"), encoding="ascii")
(WORK / "unit.py").write_text(generate_py(parsed, resolved, "unit").module,
                              encoding="ascii")
DRIVER = compile(differ.generate(parsed, resolved, "python"),
                 str(WORK / "py_driver.py"), "exec")
sys.path.insert(0, str(WORK))
AHEAD = differ.schema_prefix(resolved)
ARGUED = {name: [value for _, value in differ.argued(struct)]
          for name, struct in resolved.structs.items()
          if differ.argued(struct)}


def compiled(data: bytes) -> str:
	"""What the generated reader's driver prints for `data`."""
	out = io.StringIO()
	sys.argv = ["py_driver.py", data.hex()]
	with contextlib.redirect_stdout(out):
		try:
			exec(DRIVER, {"__name__": "__main__"})
		except SystemExit as stop:
			if stop.code not in (None, 0):
				raise AssertionError(f"the driver exited {stop.code}") \
					from None
	return out.getvalue()


def one(data: bytes) -> None:
	said = by_member(compiled(data))
	walked = by_member(report.listing(image, data, prefix=AHEAD,
	                                  argued=ARGUED))
	walked = {((f"argued {where}" if where in ARGUED else where), member): line
	          for (where, member), line in walked.items()}

	def refused(where: str) -> bool:
		mine = walked.get((where, "validate"))
		return mine is not None and mine != "validate 0" \
			and mine == said.get((where, "validate"))

	bad = [(key, walked[key], said[key])
	       for key in sorted(walked.keys() & said.keys())
	       if walked[key] != said[key]
	       and not (" count=" in walked[key] and refused(key[0]))]
	unanswered = sorted(key for key in said if key[1].endswith(".sum")
	                    and key not in walked)
	if bad or unanswered:
		raise AssertionError(f"{SCHEMA.name} {data.hex()}: the walk and "
		                     f"the reader disagree: {bad} {unanswered}")


atheris.Setup([sys.argv[0], *sys.argv[3:]], one)
atheris.Fuzz()
