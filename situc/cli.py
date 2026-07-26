"""Command-line entry point.

Only the subcommands the current phase supports are wired up. The rest of the
surface in project.md section 21 is declared here as it arrives, so that an
unimplemented command reports its phase rather than an argparse error.
"""

from __future__ import annotations

import argparse
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
	"build":		4,
	"gen-tests":		4,
	"gen-fuzz":		4,
	"advise":		9,
	"diff":			9,
	"doc":			9,
	"gen-dissector":	9,
	"gen-codec-tests":	7,
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

	map_cmd = sub.add_parser("map", help="emit the capability map")
	map_cmd.add_argument("schema", type=Path)
	map_cmd.add_argument("--format", choices=("text", "summary"), default="text",
	                     help="the committable map, or a per-struct digest")

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

	if args.format == "summary":
		sys.stdout.write(capmap.summary(resolved))
	else:
		sys.stdout.write(capmap.render(schema, resolved, source.path))

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
		print(f"situc: `{args.command}` is not yet implemented; "
		      f"planned for phase {phase} (project.md section 26)", file=sys.stderr)
		return 2

	commands = {
		"dump-ast": cmd_dump_ast,
		"map":      cmd_map,
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
