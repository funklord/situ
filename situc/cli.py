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

from situc import __version__, ast, capmap, requirements
from situc import layers
from situc.layers import LAYERS
from situc.diagnostics import Diagnostic, Source, SituError
from situc.dump import dump
from situc.layout import solve
from situc.parser import parse
from situc.resolve import ResolvedSchema, resolve
from situc.unparse import unparse

# Subcommands named in section 21 but not yet built, with the phase that adds
# each one. Listed so `situc advise` says "phase 9" rather than "invalid choice".
#: Subcommands named in section 21 but not yet built, with the phase that adds
#: each one. Every one of them has now landed; the mechanism stays because the
#: parser needs a name to be a choice before it can explain itself, and the
#: next command to be planned will want it.
FUTURE_COMMANDS: dict[str, int] = {}

#: Rungs that are decided and not built, with the phase that adds each. A
#: choice the parser accepts and the compiler then refuses by name beats
#: "invalid choice", which tells a reader the rung does not exist rather than
#: that it has not arrived -- the same reasoning as FUTURE_COMMANDS above.
FUTURE_LAYERS: dict[str, str] = {
}


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		prog        = "situc",
		description = "compiler for situ schemas",
	)
	parser.add_argument("--diagnostics", choices=("text", "json"), default="text",
	                    help="diagnostic output format")

	# A packaged tool that cannot say which version it is cannot be evaluated,
	# and the number comes from the package rather than from a second copy
	# here: `situc/__init__.py` is what the Debian packaging reads too.
	parser.add_argument("--version", action="version",
	                    version=f"situc {__version__}",
	                    help="print the version and exit")

	sub = parser.add_subparsers(dest="command", required=True)

	dump_cmd = sub.add_parser("dump-ast", help="print the parsed AST")
	dump_cmd.add_argument("schema", type=Path)
	dump_cmd.add_argument("--format", choices=("tree", "source"), default="tree",
	                      help="structural dump, or the AST rendered back to situ source")

	build_cmd = sub.add_parser("build", help="generate accessor code")
	build_cmd.add_argument("schema", type=Path)
	build_cmd.add_argument("--out", type=Path, default=Path("."),
	                       help="output directory (default: the current one)")
	build_cmd.add_argument("--target", choices=("c", "cpp", "python", "rust"),
	                       default="c",
	                       help="backend; rust arrives in phase 11")
	build_cmd.add_argument("--prefix", default="situ",
	                       help="identifier prefix for generated symbols")
	build_cmd.add_argument("--single-file", action="store_true",
	                       help="inline the parts of the Python runtime this "
	                            "schema reaches, so the output is one module "
	                            "importing only the standard library "
	                            "(python only; 26.70)")
	build_cmd.add_argument("--owned", action="store_true",
	                       help="also emit a fixed-size C struct per layout "
	                            "and a decode that copies into it, for callers "
	                            "that hold the value after the bytes are gone "
	                            "(C only; 26.69)")
	build_cmd.add_argument("--materialize", action="store_true",
	                       help="also emit the second accessor family: an "
	                            "index over each capped run, so reaching an "
	                            "element is arithmetic rather than a walk "
	                            "(decision 0022)")
	build_cmd.add_argument("--layer", choices=LAYERS, default="view",
	                       help="how much of the schema becomes code. Each rung "
	                            "emits everything below it: `view` is accessors "
	                            "over bytes the caller owns, `relate` adds a "
	                            "predicate per relation (decision 0032)")

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

	tamper_cmd = sub.add_parser(
		"gen-tamper", help="generate the harness that watches a tag's gate refuse")
	tamper_cmd.add_argument("schema", type=Path)
	tamper_cmd.add_argument("--out", type=Path, default=Path("."))
	tamper_cmd.add_argument("--prefix", default="situ")

	proto_cmd = sub.add_parser(
		"import-proto", help="import a .proto as a description of its wire format")
	proto_cmd.add_argument("proto", type=Path)
	proto_cmd.add_argument("-o", "--out", type=Path, required=True)
	proto_cmd.add_argument("--accept-lossy", action="store_true",
	                       help="take the schema anyway, with the fidelity "
	                            "report as warnings")

	derived_cmd = sub.add_parser(
		"gen-derived", help="generate implementations from kernel descriptions")
	derived_cmd.add_argument("schema", type=Path)
	derived_cmd.add_argument("--out", type=Path, default=Path("."))
	derived_cmd.add_argument("--prefix", default="situ")

	checks_cmd = sub.add_parser(
		"gen-checks",
		help="generate tests holding the accessors to the capability map")
	checks_cmd.add_argument("schema", type=Path)
	checks_cmd.add_argument("--out", type=Path, default=Path("."))
	checks_cmd.add_argument("--prefix", default="situ")

	doc_cmd = sub.add_parser(
		"doc", help="RFC-style byte-layout diagrams and a field reference")
	doc_cmd.add_argument("schema", type=Path)
	doc_cmd.add_argument("--format", choices=("ascii", "markdown"), default="ascii",
	                     help="plain text, or markdown with fenced diagrams")
	doc_cmd.add_argument("--out", type=Path, default=None,
	                     help="write to a file in this directory instead of stdout")

	sub.add_parser(
		"lsp", help="run a language server over stdio (section 26.19)")

	dissector_cmd = sub.add_parser(
		"gen-dissector", help="generate a Wireshark dissector in Lua")
	dissector_cmd.add_argument("schema", type=Path)
	dissector_cmd.add_argument("--out", type=Path, default=None,
	                           help="write to a file in this directory instead of stdout")

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

	wire_cmd = sub.add_parser(
		"wire", help="emit the byte-level contract, or check the committed one")
	wire_cmd.add_argument("schema", type=Path)
	wire_cmd.add_argument("--check", action="store_true",
	                      help="compare against the committed .situ.wire and "
	                           "classify what changed")

	map_cmd = sub.add_parser("map", help="emit the capability map")
	map_cmd.add_argument("schema", type=Path)
	map_cmd.add_argument("--format", choices=("text", "summary"), default="text",
	                     help="the committable map, or a per-struct digest")
	map_cmd.add_argument("--check", action="store_true",
	                     help="compare against the committed *.situ.map and fail "
	                          "if it differs")

	pack_cmd = sub.add_parser(
		"pack", help="emit the packed layout image a walker reads")
	pack_cmd.add_argument("schema", type=Path)
	pack_cmd.add_argument("-o", "--out", type=Path,
	                      help="write here rather than to stdout, which is "
	                           "binary and should not be piped by accident")
	pack_cmd.add_argument("--metadata", action="store_true",
	                      help="append names and capability vectors, which a "
	                           "tooling walker wants and a device does not")
	pack_cmd.add_argument("--coverage", action="store_true",
	                      help="report what went into the image, and what "
	                           "could not be encoded, rather than the image")

	verify_cmd = sub.add_parser(
		"verify", help="check that real bytes conform to the schema")
	verify_cmd.add_argument("schema", type=Path)
	verify_cmd.add_argument("vectors", type=Path)

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


def cmd_pack(args: argparse.Namespace) -> int:
	"""`situc pack schema.situ -o schema.situ.image` -- 26.33, decision 0026.

	Emits the image; nothing here walks one. The `--coverage` report exists
	because an image is opaque: a packer that quietly failed to encode a size
	expression produces a file that loads and walks and computes the wrong
	length, and the only honest way to ship that is to say what went in.
	"""
	from situc import pack as packer

	source   = read_source(args.schema)
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))
	image, coverage = packer.pack(schema, resolved, metadata=args.metadata)

	if args.coverage:
		print(f"structs      {coverage.structs}")
		print(f"placements   {coverage.placements}")
		print(f"expressions  {coverage.expressions}")
		if coverage.relations:
			print(f"relations    {coverage.relations}")
		print(f"image bytes  {len(image)}")
		for family, count in sorted(coverage.carried.items()):
			state = "dropped" if family in coverage.unencoded else "encoded"
			print(f"  {family:<10} {count:4d}  {state}")
		for path, why in sorted(coverage.unencodable.items()):
			print(f"unencodable  {path}: {why}")
		return 1 if coverage.unencodable or coverage.unencoded else 0

	if args.out is None:
		if sys.stdout.isatty():
			print("situc: the image is binary; use -o or redirect it",
			      file=sys.stderr)
			return 2
		sys.stdout.buffer.write(image)
	else:
		args.out.write_bytes(image)
		print(f"situc: wrote {args.out} ({len(image)} bytes)", file=sys.stderr)
	return 0


def cmd_wire(args: argparse.Namespace) -> int:
	"""`situc wire schema.situ` -- what the bytes mean (section 19.3).

	Committed and checked for the reason the map is: a wire break should be a
	red diff at the moment somebody edits the schema, not something a peer
	discovers in production. Unlike the map, the failure here is not a cost --
	it is a message that no longer parses on a machine nobody can recompile.
	"""
	from situc import wire

	source   = read_source(args.schema)
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))
	current  = wire.render(schema, resolved, source.path)

	if not args.check:
		sys.stdout.write(current)
		return 0

	committed = args.schema.with_suffix(".situ.wire")
	if not committed.exists():
		print(f"situc: no committed signature at {committed}", file=sys.stderr)
		print(f"situc: create it with `situc wire {args.schema} > {committed}`",
		      file=sys.stderr)
		return 1

	before = committed.read_text(encoding="ascii")
	if before == current:
		print(f"situc: {committed} is current", file=sys.stderr)
		return 0

	verdict = wire.compare(before, current)
	sys.stdout.write(wire.render_verdict(verdict))

	if verdict.breaking:
		print(f"situc: the wire contract of {args.schema.name} is not "
		      "backward compatible", file=sys.stderr)
		print("situc: a deployed peer will misread messages from this build",
		      file=sys.stderr)
		return 1

	print(f"situc: the wire contract of {args.schema.name} changed compatibly",
	      file=sys.stderr)
	print(f"situc: review the above, then run "
	      f"`situc wire {args.schema} > {committed}`", file=sys.stderr)
	return 1


def cmd_verify(args: argparse.Namespace) -> int:
	"""`situc verify schema.situ corpus.vectors` -- the schema as a
	specification, checked against bytes somebody else's implementation
	produced.

	For a project that cannot take situ's usual bargain. `wire --check` and
	`map --check` hold a schema to its own committed contracts and never look
	at a real byte, so neither notices a schema that disagreed with the
	implementation from the day it was written. This does, and it generates
	nothing into the caller's tree.
	"""
	from situc import verify

	source   = read_source(args.schema)
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))

	found = verify.check(schema, resolved, args.schema.stem,
	                     read_source(args.vectors))
	sys.stdout.write(verify.render(found, str(args.schema), str(args.vectors)))

	return 0 if found and all(one.ok for one in found) else 1


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
	"""`situc build schema.situ [--target=c|cpp]` -- generate accessors.

	Both backends read the same layout and the same capability vectors; what
	differs is how much of them the target language can enforce rather than
	document (section 20.1).
	"""
	from situc.codegen.c import generate
	from situc.codegen.cpp import generate as generate_cpp

	source, resolved, outcomes = analyse(args.schema)
	files: dict[str, str]
	warnings: list[Diagnostic]

	if args.single_file and args.target != "python":
		raise SystemExit(
			f"situc: --single-file inlines the Python runtime and --target is "
			f"{args.target}. The C, C++ and Rust outputs already carry their "
			f"runtime as a header or a module the build compiles in.")

	if args.owned and args.target != "c":
		raise SystemExit(
			f"situc: --owned is a C construct and --target is {args.target}. "
			f"The other backends already hand back owned values: Rust and "
			f"Python return them by value, and C++ has no view-only accessor "
			f"a caller cannot copy.")

	# The layer boundary, made real. A construct whose output extent cannot be
	# known without transforming it has nowhere to put the result at rung 1,
	# so asking for it there is refused rather than emitted wrong.
	needy = layers.allocating(parse(source))
	if needy and LAYERS.index(args.layer) < LAYERS.index("edit"):
		named = ", ".join(f"`{one}`" for one in sorted(needy))
		raise SystemExit(
			f"situc: --layer {args.layer} cannot emit {named}. Its codec "
			f"expands without a bound, so the output extent is not known "
			f"until the transform has run and rung 1 has nowhere to put it. "
			f"Build it at `--layer edit`, which is what that rung is for "
			f"(0031 case E, decision 0032).")

	if args.layer in FUTURE_LAYERS:
		raise SystemExit(
			f"situc: --layer {args.layer} is decided and not built; phase "
			f"{FUTURE_LAYERS[args.layer]} adds it. Decision 0032 has the rung "
			f"ladder and what each one may assume; `--layer view` is what "
			f"ships and is the default.")

	if args.target == "rust":
		from situc.codegen.rust import generate as generate_rs

		emitted_rs = generate_rs(parse(source), resolved, args.schema.stem,
		                         args.prefix,
							 materialize=args.materialize)
		files    = emitted_rs.files()
		warnings = emitted_rs.warnings
	elif args.target == "python":
		from situc.codegen.python import generate as generate_py

		emitted_py = generate_py(parse(source), resolved, args.schema.stem,
		                         args.prefix,
							 materialize=args.materialize)
		files    = emitted_py.files()
		warnings = emitted_py.warnings

		if args.single_file:
			from situc.codegen.python import single

			name        = f"{args.schema.stem}.py"
			files[name] = single.inline(files[name], args.schema.stem)
	elif args.target == "cpp":
		cpp      = generate_cpp(parse(source), resolved, args.schema.stem,
		                        args.prefix,
							 materialize=args.materialize)
		files    = cpp.files()
		warnings = cpp.warnings
	else:
		parsed   = parse(source)
		emitted  = generate(parsed, resolved, args.schema.stem,
		                    args.prefix, materialize=args.materialize)
		files    = emitted.files()
		warnings = emitted.warnings

		if args.owned:
			from situc.codegen.c import owned

			files.update(owned.generate(parsed, resolved, args.schema.stem,
			                            args.prefix))
			# Refusals are printed rather than left as an absence: a caller
			# who asked for this mode and found their struct missing would
			# conclude the generator was broken.
			for name, why in owned.refusals(resolved):
				print(f"situc: no owned form for `{name}`: {why}",
				      file=sys.stderr)

	if args.layer in ("edit", "relate", "frame", "converse", "drive"):
		from situc.codegen.c import edit
		from situc.codegen.cpp import edit as edit_cpp
		from situc.codegen.python import edit as edit_py
		from situc.codegen.rust import edit as edit_rs

		parsed = parse(source)
		emit = {"cpp": edit_cpp.generate, "rust": edit_rs.generate,
		        "python": edit_py.generate}.get(args.target)
		files.update(emit(parsed, resolved, args.schema.stem) if emit
		             else edit.generate(parsed, resolved, args.schema.stem,
		                                args.prefix))
		for name, why in edit.refusals(resolved):
			print(f"situc: no owned form for `{name}` at any rung: {why}",
			      file=sys.stderr)

	if args.layer in ("relate", "frame", "converse", "drive"):
		files.update(_relate(parse(source), resolved, args))

	if args.layer in ("frame", "converse", "drive"):
		files.update(_frame(parse(source), resolved, args))

	if args.layer in ("converse", "drive"):
		from situc.codegen.c import converse

		parsed = parse(source)
		files.update(_converse(parsed, resolved, args))
		for name, why in converse.refusals(parsed, resolved):
			print(f"situc: no conversation table for `{name}`: {why}",
			      file=sys.stderr)

	if args.layer == "drive":
		from situc.codegen.c import drive
		from situc.codegen.cpp import drive as drive_cpp
		from situc.codegen.python import drive as drive_py
		from situc.codegen.rust import drive as drive_rs

		parsed = parse(source)
		emit = {"cpp": drive_cpp.generate, "rust": drive_rs.generate,
		        "python": drive_py.generate}.get(args.target)
		files.update(emit(parsed, resolved, args.schema.stem) if emit
		             else drive.generate(parsed, resolved, args.schema.stem,
		                                 args.prefix))
		for name, why in drive.refusals(parsed, resolved):
			print(f"situc: no driver for relation `{name}`: {why}",
			      file=sys.stderr)

	args.out.mkdir(parents=True, exist_ok=True)
	for name, text in files.items():
		(args.out / name).write_text(text, encoding="ascii")
		print(f"situc: wrote {args.out / name}", file=sys.stderr)

	_report(args, warnings + requirements.warnings(outcomes)
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


def cmd_gen_tamper(args: argparse.Namespace) -> int:
	"""Emit the harness that demonstrates the gate refuses (26.131).

	A gate nobody has watched fail is not evidence. The verifier is the
	caller's; the geometry -- which bytes must matter and which must not --
	is the schema's, and generating the flips from it is what keeps the
	demonstration honest as coverage changes.
	"""
	from situc.codegen.c import tamper

	source   = read_source(args.schema)
	schema   = parse(source)
	resolved = resolve(schema, solve(schema))
	name     = args.schema.stem
	files    = tamper.generate(schema, resolved, name, args.prefix)

	if not files:
		print(f"situc: {args.schema} carries no tag; nothing to tamper with",
		      file=sys.stderr)
		return 0

	args.out.mkdir(parents=True, exist_ok=True)
	for filename, text in files.items():
		target = args.out / filename
		target.write_text(text, encoding="ascii")
		print(f"situc: wrote {target}", file=sys.stderr)
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


def cmd_import_proto(args: argparse.Namespace) -> int:
	"""`situc import-proto foo.proto -o foo.situ` (section 19.2).

	The fidelity report is the feature. An importer that silently produced a
	plausible-looking schema would be worse than no importer, because the user
	would trust it -- so anything with no expression in a byte layout is named
	with its location, and the run fails unless the user says otherwise.
	"""
	from situc import proto

	text     = args.proto.read_text(encoding="utf-8")
	imported = proto.read(text)
	name     = args.proto.name

	if imported.lossy:
		print(proto.report(imported, name), file=sys.stderr)
		if not args.accept_lossy:
			print(f"situc: refusing to write {args.out} from a lossy import",
			      file=sys.stderr)
			return 1

	if not imported.messages:
		print(f"situc: {name} declares no messages", file=sys.stderr)
		return 1

	schema = proto.translate(imported, name)
	args.out.write_text(schema, encoding="ascii")

	fields = sum(len(message.fields) for message in imported.messages)
	print(f"situc: wrote {args.out} "
	      f"({len(imported.messages)} message(s), {fields} field(s))",
	      file=sys.stderr)

	# Written, and then read back: an importer that emits a schema its own
	# compiler rejects has produced nothing, and finding that out here is much
	# cheaper than finding it out later.
	try:
		parse(read_source(args.out))
	except SituError as exc:
		print(f"situc: the imported schema does not compile", file=sys.stderr)
		_report(args, [exc.diagnostic])
		return 1

	return 0


def cmd_gen_derived(args: argparse.Namespace) -> int:
	"""Emit the code a kernel description implies (section 26.12).

	The properties in the capability map and the implementation here come from
	one description, which is the entire difference between the two codec
	tiers: a tier-1 signature is a promise nobody checked, and a tier-2 one is
	computed from the same source as the code.
	"""
	from situc.codegen.c import derived

	source = read_source(args.schema)
	schema = parse(source)
	name   = args.schema.stem
	text   = derived.generate(schema, name, args.prefix)

	args.out.mkdir(parents=True, exist_ok=True)
	target = args.out / f"{name}_derived.c"
	target.write_text(text, encoding="ascii")

	count = sum(1 for impl in schema.impls()
	            if impl.kind is ast.ImplKind.DERIVED)
	print(f"situc: wrote {target} ({count} derived binding(s))", file=sys.stderr)
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


def cmd_doc(args: argparse.Namespace) -> int:
	"""`situc doc schema.situ` -- the layout as documentation (section 20.3).

	A hand-drawn packet diagram is a second description of the layout, and
	second descriptions drift. This one is rendered from the placements the
	accessors are generated from, so it cannot.
	"""
	from situc import doc

	source, resolved, _ = analyse(args.schema)
	name = args.schema.stem
	text = doc.render(parse(source), resolved, name, args.format)

	if args.out is None:
		sys.stdout.write(text)
		return 0

	args.out.mkdir(parents=True, exist_ok=True)
	suffix = ".md" if args.format == "markdown" else ".txt"
	target = args.out / f"{name}{suffix}"
	target.write_text(text, encoding="ascii")
	print(f"situc: wrote {target}")
	return 0


def cmd_lsp(args: argparse.Namespace) -> int:
	"""`situc lsp` -- a language server over stdio (section 26.19).

	The capability vector of the field under the cursor and the blame chain for
	a failing requirement are already computed; this is them behind a door an
	editor can open.
	"""
	from situc import lsp

	del args
	return lsp.main()


def cmd_gen_dissector(args: argparse.Namespace) -> int:
	"""`situc gen-dissector schema.situ` -- a Wireshark dissector (section 20.3).

	A hand-written dissector is a third description of the layout, after the
	schema and the accessors, and the one nobody remembers to update. This one
	comes from the placements, so it cannot drift from the parser.
	"""
	from situc import dissector

	source, resolved, _ = analyse(args.schema)
	name = args.schema.stem
	text = dissector.generate(parse(source), resolved, name)

	if args.out is None:
		sys.stdout.write(text)
		return 0

	args.out.mkdir(parents=True, exist_ok=True)
	target = args.out / f"{name}.lua"
	target.write_text(text, encoding="ascii")
	print(f"situc: wrote {target}")
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


def _frame(schema: ast.Schema, resolved: ResolvedSchema,
		args: argparse.Namespace) -> dict[str, str]:
	"""Rung 4's reader for whichever backend was asked for.

	The four differ in shape here and not only in spelling: Rust answers a
	`Framing` enum rather than an error code, C++ acquires a view through an
	`rt::message` it has to own, and Python imposes no capacity because a
	`bytearray` grows. Each is written against its own runtime rather than
	translated from C.
	"""
	from situc.codegen.c import frame as frame_c
	from situc.codegen.cpp import frame as frame_cpp
	from situc.codegen.python import frame as frame_py
	from situc.codegen.rust import frame as frame_rs

	if args.target == "cpp":
		return frame_cpp.generate(schema, resolved, args.schema.stem)
	if args.target == "rust":
		return frame_rs.generate(schema, resolved, args.schema.stem)
	if args.target == "python":
		return frame_py.generate(schema, resolved, args.schema.stem)
	return frame_c.generate(schema, resolved, args.schema.stem, args.prefix)


def _converse(schema: ast.Schema, resolved: ResolvedSchema,
		args: argparse.Namespace) -> dict[str, str]:
	"""Rung 5's table for whichever backend was asked for.

	`match` is a keyword in Rust, so the taking half is `take` there -- and
	Python's capacity is required rather than optional, unlike its framing
	reader: a buffer's size is about representation and a pending table's
	bound is about refusing somebody who opens exchanges and never answers.
	"""
	from situc.codegen.c import converse as conv_c
	from situc.codegen.cpp import converse as conv_cpp
	from situc.codegen.python import converse as conv_py
	from situc.codegen.rust import converse as conv_rs

	if args.target == "cpp":
		return conv_cpp.generate(schema, resolved, args.schema.stem)
	if args.target == "rust":
		return conv_rs.generate(schema, resolved, args.schema.stem)
	if args.target == "python":
		return conv_py.generate(schema, resolved, args.schema.stem)
	return conv_c.generate(schema, resolved, args.schema.stem, args.prefix)


def _relate(schema: ast.Schema, resolved: ResolvedSchema,
		args: argparse.Namespace) -> dict[str, str]:
	"""Rung 3's output for whichever backend was asked for.

	The four differ only in spelling: `situc.relation` decides which relations
	are expressible, so the refusal list is the same list in every backend and
	is printed once here. Reported rather than left as an absence, for the
	reason `owned` gives -- a caller who asked for the rung and found their
	predicate missing would conclude the generator was broken rather than that
	the comparison has no correct spelling.
	"""
	from situc import relation
	from situc.codegen.c import relate as relate_c
	from situc.codegen.cpp import relate as relate_cpp
	from situc.codegen.python import relate as relate_py
	from situc.codegen.rust import relate as relate_rs

	for name, why in relation.refusals(schema, resolved):
		print(f"situc: no predicate for relation `{name}`: {why}",
		      file=sys.stderr)

	if args.target == "cpp":
		return relate_cpp.generate(schema, resolved, args.schema.stem)
	if args.target == "rust":
		return relate_rs.generate(schema, resolved, args.schema.stem)
	if args.target == "python":
		return relate_py.generate(schema, resolved, args.schema.stem)
	return relate_c.generate(schema, resolved, args.schema.stem, args.prefix)


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
		"wire":     cmd_wire,
		"pack":     cmd_pack,
		"map":      cmd_map,
		"verify":   cmd_verify,
		"build":    cmd_build,
		"gen-fuzz": cmd_gen_fuzz,
		"gen-checks": cmd_gen_checks,
		"gen-derived": cmd_gen_derived,
		"import-proto": cmd_import_proto,
		"gen-tests": cmd_gen_tests,
		"gen-codec-tests": cmd_gen_codec_tests,
		"gen-tamper": cmd_gen_tamper,
		"explain":  cmd_explain,
		"doc":      cmd_doc,
		"gen-dissector": cmd_gen_dissector,
		"lsp":      cmd_lsp,
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
