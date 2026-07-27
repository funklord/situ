"""Command-line entry point.

Only the subcommands the current phase supports are wired up. The rest of the
surface in project.md section 21 is declared here as it arrives, so that an
unimplemented command reports its phase rather than an argparse error.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

from situc import capmap, requirements
from situc.diagnostics import Diagnostic, Source, SituError
from situc.dump import dump
from situc.layout import solve
from situc.parser import parse
from situc.resolve import ResolvedSchema, resolve
from situc.unparse import unparse

# Subcommands named in section 21 but not yet built, with the phase that adds
# each one. Listed so `situc advise` says "phase 9" rather than "invalid choice".
FUTURE_COMMANDS = {
	"doc":			26,	# section 26.14, "Beyond"
	"gen-dissector":	26,
	"import-proto":		13,
}


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		prog        = "situc",
		description = "compiler for situ schemas",
	)
	parser.add_argument("--diagnostics", choices=("text", "json"), default="text",
	                    help="diagnostic output format")

	sub = parser.add_subparsers(dest="command", required=True)

	dump_cmd = sub.add_parser("dump-ast", help="print the parsed AST")
	dump_cmd.add_argument("schema", type=Path)
	dump_cmd.add_argument("--format", choices=("tree", "source"), default="tree",
	                      help="structural dump, or the AST rendered back to situ source")

	build_cmd = sub.add_parser("build", help="generate accessor code")
	build_cmd.add_argument("schema", type=Path)
	build_cmd.add_argument("--out", type=Path, default=Path("."),
	                       help="output directory (default: the current one)")
	build_cmd.add_argument("--target", choices=("c",), default="c",
	                       help="backend; rust arrives in phase 11")
	build_cmd.add_argument("--prefix", default="situ",
	                       help="identifier prefix for generated symbols")

	tests_cmd = sub.add_parser("gen-tests", help="generate golden-vector tests")
	tests_cmd.add_argument("schema", type=Path)
	tests_cmd.add_argument("vectors", type=Path)
	tests_cmd.add_argument("--out", type=Path, default=Path("."))
	tests_cmd.add_argument("--prefix", default="situ")

	codec_cmd = sub.add_parser(
		"gen-codec-tests", help="generate property tests from codec signatures")
	codec_cmd.add_argument("schema", type=Path)
	codec_cmd.add_argument("--out", type=Path, default=Path("."))
	codec_cmd.add_argument("--prefix", default="situ")

	checks_cmd = sub.add_parser(
		"gen-checks",
		help="generate tests holding the accessors to the capability map")
	checks_cmd.add_argument("schema", type=Path)
	checks_cmd.add_argument("--out", type=Path, default=Path("."))
	checks_cmd.add_argument("--prefix", default="situ")

	fuzz_cmd = sub.add_parser("gen-fuzz", help="generate a fuzz harness")
	fuzz_cmd.add_argument("schema", type=Path)
	fuzz_cmd.add_argument("--out", type=Path, default=Path("."))
	fuzz_cmd.add_argument("--prefix", default="situ")

	advise_cmd = sub.add_parser("advise", help="ranked, costed schema suggestions")
	advise_cmd.add_argument("schema", type=Path)
	advise_cmd.add_argument("--format", choices=("text", "json"), default="text")

	diff_cmd = sub.add_parser("diff", help="capability changes between two revisions")
	diff_cmd.add_argument("old", type=Path)
	diff_cmd.add_argument("new", type=Path)
	diff_cmd.add_argument("--format", choices=("text", "json"), default="text")

	map_cmd = sub.add_parser("map", help="emit the capability map")
	map_cmd.add_argument("schema", type=Path)
	map_cmd.add_argument("--format", choices=("text", "summary"), default="text",
	                     help="the committable map, or a per-struct digest")
	map_cmd.add_argument("--check", action="store_true",
	                     help="compare against the committed *.situ.map and fail "
	                          "if it differs")

	explain_cmd = sub.add_parser(
		"explain", help="one path's capability vector and its blame chains")
	explain_cmd.add_argument("schema", type=Path)
	explain_cmd.add_argument("path", help="a struct or field path, e.g. Header.seq")

	for name in sorted(FUTURE_COMMANDS):
		future = sub.add_parser(name, help=f"not yet implemented (phase {FUTURE_COMMANDS[name]})")
		future.add_argument("args", nargs="*")

	return parser


def read_source(path: Path) -> Source:
	try:
		text = path.read_text(encoding="utf-8")
	except OSError as exc:
		raise SystemExit(f"situc: cannot read {path}: {exc.strerror}") from exc
	return Source(str(path), text)


def analyse(path: Path) -> tuple[Source, ResolvedSchema, list[requirements.Outcome]]:
	"""Parse, solve, resolve and discharge. The common front half of every
	command that needs more than an AST."""
	source   = read_source(path)
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))
	outcomes = requirements.discharge(schema, resolved)
	return source, resolved, outcomes


def cmd_dump_ast(args: argparse.Namespace) -> int:
	schema = parse(read_source(args.schema))
	output = unparse(schema) if args.format == "source" else dump(schema)
	sys.stdout.write(output)
	return 0


def cmd_map(args: argparse.Namespace) -> int:
	source   = read_source(args.schema)
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))

	# Requirements are discharged before the map is printed: a schema whose
	# budget is blown should fail rather than emit a map recording the breach.
	outcomes = requirements.discharge(schema, resolved)

	if args.check:
		return _check_map(args, capmap.render(schema, resolved, source.path),
		                  outcomes)

	if args.format == "summary":
		sys.stdout.write(capmap.summary(resolved))
	else:
		sys.stdout.write(capmap.render(schema, resolved, source.path))

	_report(args, requirements.warnings(outcomes) + requirements.deferrals(outcomes))
	return 0


def _check_map(args: argparse.Namespace, current: str,
		outcomes: list[requirements.Outcome]) -> int:
	"""Compare against the committed map (section 18.1).

	The point of committing the map is that a capability regression arrives as
	a reviewable diff at the moment of editing rather than as a performance
	surprise months later. That only works if something fails when the two
	disagree, so this is the something.
	"""
	committed = args.schema.with_suffix(".situ.map")

	if not committed.exists():
		print(f"situc: no committed map at {committed}", file=sys.stderr)
		print(f"situc: create it with `situc map {args.schema} > {committed}`",
		      file=sys.stderr)
		return 1

	expected = committed.read_text(encoding="ascii")
	if expected == current:
		print(f"situc: {committed} is current", file=sys.stderr)
		_report(args, requirements.warnings(outcomes)
		        + requirements.deferrals(outcomes))
		return 0

	difference = difflib.unified_diff(
		expected.splitlines(keepends=True), current.splitlines(keepends=True),
		fromfile=str(committed), tofile="(this build)")
	sys.stdout.writelines(difference)

	print(f"situc: the capability map of {args.schema.name} has changed",
	      file=sys.stderr)
	print("situc: review the diff above, then run "
	      f"`situc map {args.schema} > {committed}`", file=sys.stderr)
	return 1


def cmd_advise(args: argparse.Namespace) -> int:
	"""`situc advise schema.situ` -- the section 18.2 catalog, ranked and costed.

	Exit status stays 0 whatever it finds. A suggestion is advice about a
	design, not a verdict on one: a schema may have excellent reasons for every
	construct the catalog would change, and a build that failed on advice would
	teach people to stop reading it.
	"""
	from situc import advise

	_, resolved, outcomes = analyse(args.schema)
	suggestions = advise.suggest(resolved)

	if args.format == "json":
		payload = {"suggestions": [advise.to_dict(item) for item in suggestions]}
		json.dump(payload, sys.stdout, indent=2)
		sys.stdout.write("\n")
	else:
		sys.stdout.write(advise.render(suggestions))

	_report(args, requirements.warnings(outcomes) + requirements.deferrals(outcomes))
	return 0


def cmd_diff(args: argparse.Namespace) -> int:
	"""`situc diff old.situ new.situ` -- what a revision cost (section 18.3).

	Exits non-zero on a regression, and only on a regression: this is meant for
	code review and CI, where the useful signal is "this edit took something
	away" rather than "these files differ".
	"""
	from situc import revision

	old = _resolve_for_diff(args.old)
	new = _resolve_for_diff(args.new)
	changes = revision.compare(old, new)

	if args.format == "json":
		payload = {"changes": [revision.to_dict(change) for change in changes]}
		json.dump(payload, sys.stdout, indent=2)
		sys.stdout.write("\n")
	else:
		sys.stdout.write(revision.render(changes))

	return 1 if any(change.is_regression for change in changes) else 0


def _resolve_for_diff(path: Path) -> ResolvedSchema:
	"""Resolve without discharging requirements.

	A revision whose budget is blown is exactly the one worth diffing, and
	refusing to describe it would withhold the explanation at the moment it is
	wanted.
	"""
	source = read_source(path)
	schema = parse(source)
	return resolve(schema, solve(schema))


def cmd_build(args: argparse.Namespace) -> int:
	from situc.codegen.c import generate

	source, resolved, outcomes = analyse(args.schema)
	generated = generate(parse(source), resolved, args.schema.stem, args.prefix)

	args.out.mkdir(parents=True, exist_ok=True)
	for name, text in generated.files().items():
		(args.out / name).write_text(text, encoding="ascii")
		print(f"situc: wrote {args.out / name}", file=sys.stderr)

	_report(args, generated.warnings + requirements.warnings(outcomes)
	        + requirements.deferrals(outcomes))
	return 0


def cmd_gen_tests(args: argparse.Namespace) -> int:
	from situc.codegen.c import vectors

	source, resolved, outcomes = analyse(args.schema)
	cases = vectors.parse_vectors(read_source(args.vectors))

	name = args.schema.stem
	try:
		text = vectors.generate(parse(source), resolved, cases, name, args.prefix)
	except ValueError as exc:
		print(f"situc: {exc}", file=sys.stderr)
		return 1

	args.out.mkdir(parents=True, exist_ok=True)
	target = args.out / f"{name}_vectors.c"
	target.write_text(text, encoding="ascii")
	print(f"situc: wrote {target} ({len(cases)} vectors)", file=sys.stderr)

	_report(args, requirements.warnings(outcomes) + requirements.deferrals(outcomes))
	return 0


def cmd_gen_codec_tests(args: argparse.Namespace) -> int:
	"""Emit the tests that would falsify a lying signature (section 13.1).

	The compiler cannot verify an implementation it never sees, but each
	declared property has a cheap falsifying test, and generating them from the
	signature costs the user nothing.
	"""
	from situc.codegen.c import codectests

	source = read_source(args.schema)
	schema = parse(source)
	name   = args.schema.stem
	text   = codectests.generate(schema, name, args.prefix)

	args.out.mkdir(parents=True, exist_ok=True)
	target = args.out / f"{name}_codec_tests.c"
	target.write_text(text, encoding="ascii")

	declared = len(schema.codecs())
	print(f"situc: wrote {target} ({declared} codec signatures)", file=sys.stderr)
	return 0


def cmd_gen_checks(args: argparse.Namespace) -> int:
	"""Emit the tests that would falsify the compiler's own output.

	`gen-codec-tests` does this for a codec signature, on the grounds that a
	property nobody can check is one nobody should trust. The backend is no
	more trustworthy than anybody else's algorithm, and the capability map is a
	far larger claim than a codec signature.
	"""
	from situc.codegen.c import checks

	source, resolved, outcomes = analyse(args.schema)
	name = args.schema.stem
	text = checks.generate(parse(source), resolved, name, args.prefix)

	args.out.mkdir(parents=True, exist_ok=True)
	target = args.out / f"{name}_checks.c"
	target.write_text(text, encoding="ascii")
	print(f"situc: wrote {target}", file=sys.stderr)

	_report(args, requirements.warnings(outcomes) + requirements.deferrals(outcomes))
	return 0


def cmd_gen_fuzz(args: argparse.Namespace) -> int:
	from situc.codegen.c import fuzz

	source, resolved, outcomes = analyse(args.schema)
	name = args.schema.stem
	text = fuzz.generate(parse(source), resolved, name, args.prefix)

	args.out.mkdir(parents=True, exist_ok=True)
	target = args.out / f"{name}_fuzz.c"
	target.write_text(text, encoding="ascii")
	print(f"situc: wrote {target}", file=sys.stderr)

	_report(args, requirements.warnings(outcomes) + requirements.deferrals(outcomes))
	return 0


def cmd_explain(args: argparse.Namespace) -> int:
	"""`situc explain Message.recs[].value` -- one field's full vector plus the
	blame chain for every axis not at its strongest value (section 18.2)."""
	from situc.capability import DOMAINS

	_, resolved, _ = analyse(args.schema)

	entry = resolved.find(args.path)
	if entry is None:
		struct = resolved.find_struct(args.path)
		if struct is None:
			print(f"situc: unknown path `{args.path}`", file=sys.stderr)
			return 1
		print(f"struct {struct.name}")
		for axis, value in struct.vector.items():
			print(f"  {axis.value:10} {value.render()}")
		return 0

	print(entry.placement.path)
	for axis, value in entry.vector.items():
		marker = "" if value.base == DOMAINS[axis][0] else "  <- weakened"
		print(f"  {axis.value:10} {value.render()}{marker}")

	if entry.weakenings:
		print("\nblame:")
		for weakening in entry.weakenings:
			print(f"  {weakening.effect.axis.value} := {weakening.effect.value.render()}")
			print(f"    {weakening.rule.construct}")
			print(f"    {weakening.effect.because}")
			if weakening.rule.remedy:
				print(f"    remedy: {weakening.rule.remedy}")

	return 0


def _report(args: argparse.Namespace, diagnostics: list[Diagnostic]) -> None:
	if not diagnostics:
		return

	if args.diagnostics == "json":
		payload = {"diagnostics": [d.to_dict() for d in diagnostics]}
		print(json.dumps(payload, indent=2), file=sys.stderr)
	else:
		for diagnostic in diagnostics:
			print(diagnostic.render(), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)

	if args.command in FUTURE_COMMANDS:
		phase = FUTURE_COMMANDS[args.command]
		where = ("section 26.14, \"Beyond\"" if phase == 26
		         else f"phase {phase} (project.md section 26)")
		print(f"situc: `{args.command}` is not yet implemented; "
		      f"planned for {where}", file=sys.stderr)
		return 2

	commands = {
		"dump-ast": cmd_dump_ast,
		"advise":   cmd_advise,
		"diff":     cmd_diff,
		"map":      cmd_map,
		"build":    cmd_build,
		"gen-fuzz": cmd_gen_fuzz,
		"gen-checks": cmd_gen_checks,
		"gen-tests": cmd_gen_tests,
		"gen-codec-tests": cmd_gen_codec_tests,
		"explain":  cmd_explain,
	}

	handler = commands.get(args.command)
	if handler is None:
		raise AssertionError(f"unhandled command {args.command}")

	try:
		return handler(args)
	except SituError as exc:
		_report(args, [exc.diagnostic])
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
