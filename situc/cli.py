"""Command-line entry point.

Only the phase 1 subcommands are wired up. The rest of the surface in
project.md section 21 is declared here as it arrives, so that an unimplemented
command reports its phase rather than an argparse error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from situc import capmap, requirements
from situc.diagnostics import Source, SituError
from situc.dump import dump
from situc.layout import solve
from situc.parser import parse
from situc.unparse import unparse

# Subcommands named in section 21 but not yet built, with the phase that adds
# each one. Listed so `situc advise` says "phase 9" rather than "invalid choice".
FUTURE_COMMANDS = {
	"build":		4,
	"gen-tests":		4,
	"gen-fuzz":		4,
	"explain":		9,
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
	                    help="diagnostic output format (json arrives in phase 3)")

	sub = parser.add_subparsers(dest="command", required=True)

	dump_cmd = sub.add_parser("dump-ast", help="print the parsed AST")
	dump_cmd.add_argument("schema", type=Path)
	dump_cmd.add_argument("--format", choices=("tree", "source"), default="tree",
	                      help="structural dump, or the AST rendered back to situ source")

	map_cmd = sub.add_parser("map", help="emit the capability map")
	map_cmd.add_argument("schema", type=Path)
	map_cmd.add_argument("--format", choices=("text", "summary"), default="text",
	                     help="the committable map, or a per-struct digest")

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


def cmd_dump_ast(args: argparse.Namespace) -> int:
	schema = parse(read_source(args.schema))
	output = unparse(schema) if args.format == "source" else dump(schema)
	sys.stdout.write(output)
	return 0


def cmd_map(args: argparse.Namespace) -> int:
	source = read_source(args.schema)
	schema = parse(source)
	layout = solve(schema)

	# Requirements are discharged before the map is printed: a schema whose
	# budget is blown should fail rather than emit a map recording the breach.
	outcomes = requirements.discharge(schema, layout)

	if args.format == "summary":
		sys.stdout.write(capmap.summary(layout))
	else:
		sys.stdout.write(capmap.render(schema, layout, source.path))

	for diagnostic in requirements.warnings(outcomes) + requirements.deferrals(outcomes):
		print(diagnostic.render(), file=sys.stderr)

	return 0


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)

	if args.command in FUTURE_COMMANDS:
		phase = FUTURE_COMMANDS[args.command]
		print(f"situc: `{args.command}` is not yet implemented; "
		      f"planned for phase {phase} (project.md section 26)", file=sys.stderr)
		return 2

	if args.diagnostics == "json":
		print("situc: --diagnostics=json is not yet implemented; "
		      "planned for phase 3 (project.md section 26)", file=sys.stderr)
		return 2

	commands = {
		"dump-ast": cmd_dump_ast,
		"map":      cmd_map,
	}

	handler = commands.get(args.command)
	if handler is None:
		raise AssertionError(f"unhandled command {args.command}")

	try:
		return handler(args)
	except SituError as exc:
		print(exc.diagnostic.render(), file=sys.stderr)
		return 1


if __name__ == "__main__":
	raise SystemExit(main())
