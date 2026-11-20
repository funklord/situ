"""A registry is walked from its image (0070, step 4).

The image carries a registry's cells as `image_registry_entry` rows and the
policy words they name as `image_registry_word` rows, declared in
`std/image.situ` so a program can write them without the compiler. The
Python walker's `find`, `check`, `value` and `safer` read them and walk
each cell as a message of its type, and answer every case the generated C
answers in `test_a_registry_is_generated_in_c`.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test" / "unit"))

import pytest							# noqa: E402

from situc.diagnostics import Source				# noqa: E402
from situc.layout import solve					# noqa: E402
from situc.pack import pack					# noqa: E402
from situc.parser import parse					# noqa: E402
from situc.resolve import resolve				# noqa: E402
from test_a_registry_is_generated_in_c import CASES, SCHEMA	# noqa: E402
from test_walker_c import COMPILER				# noqa: E402
from walker import registry					# noqa: E402
from walker.image import load					# noqa: E402


def _image() -> object:
	schema = parse(Source("settings.situ", SCHEMA))
	return load(pack(schema, resolve(schema, solve(schema)),
	                 metadata=True)[0])


def _ask(image: object, op: str, key: str, arg: str) -> str:
	entry = registry.find(image, key)		# type: ignore[arg-type]
	if op == "find":
		return registry.key_of(image, entry) if entry else "-"  # type: ignore[arg-type]
	assert entry is not None, key
	if op == "check":
		return str(registry.check(image, entry, arg.encode()))  # type: ignore[arg-type]
	if op == "value":
		held = registry.value(image, entry, arg.encode())  # type: ignore[arg-type]
		return str(-1 if held is None else held)
	first, second = arg.split(",")
	return str(int(registry.safer(image, entry,  # type: ignore[arg-type]
	                              first.encode(), second.encode())))


def test_the_walker_answers_what_the_c_registry_answers() -> None:
	image = _image()
	said  = [(case, _ask(image, *case[:3])) for case in CASES]
	assert [got for _, got in said] == [case[-1] for case in CASES], [
		(case, got) for case, got in said if got != case[-1]]


def test_the_rows_carry_the_policy_words_and_strings() -> None:
	image = _image()
	gone  = registry.find(image, "archive/gone")		# type: ignore[arg-type]
	assert gone is not None
	assert registry.words(image, gone, "scope") == ["estate"]	# type: ignore[arg-type]
	assert registry.words(image, gone, "display") == ["advanced"]	# type: ignore[arg-type]
	public = registry.find(image, "routing/public")	# type: ignore[arg-type]
	assert public is not None and public.safe == registry.SAFE_LOWER
	assert registry.words(image, public, "display") == ["advertised"]  # type: ignore[arg-type]


C_PROBE = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "situ_walk.h"

static situ_walk_image image;

static int row_of(const char *key, uint32_t *row)
{
	return situ_walk_registry_find(&image, NULL, (const uint8_t *)key,
	                               (uint32_t)strlen(key), row) == SITU_WALK_OK;
}

int main(int argc, char **argv)
{
	FILE *f = fopen(argv[1], "rb");
	static uint8_t blob[1 << 16];
	size_t n = fread(blob, 1, sizeof blob, f);
	fclose(f);
	if (situ_walk_open(&image, blob, (uint32_t)n) != SITU_WALK_OK)
		return 2;
	for (int i = 2; i + 2 < argc; i += 3) {
		const char *op = argv[i], *key = argv[i + 1], *arg = argv[i + 2];
		uint32_t row = 0;
		int found = row_of(key, &row);
		if (strcmp(op, "find") == 0) {
			printf("%s\n", found ? "found" : "-");
		} else if (!found) {
			printf("no-row\n");
		} else if (strcmp(op, "check") == 0) {
			situ_walk_err verdict = SITU_WALK_OK;
			if (situ_walk_registry_check(&image, row, (const uint8_t *)arg,
			                             (uint32_t)strlen(arg), &verdict)
			    != SITU_WALK_OK)
				printf("cannot-say\n");
			else
				printf("%d\n", (int)verdict);
		} else if (strcmp(op, "value") == 0) {
			int64_t out = -1;
			if (situ_walk_registry_value(&image, row, (const uint8_t *)arg,
			                             (uint32_t)strlen(arg), &out)
			    != SITU_WALK_OK)
				out = -1;
			printf("%lld\n", (long long)out);
		} else {
			const char *comma = strchr(arg, ',');
			printf("%d\n", situ_walk_registry_safer(&image, row,
			       (const uint8_t *)arg, (uint32_t)(comma - arg),
			       (const uint8_t *)comma + 1, (uint32_t)strlen(comma + 1)));
		}
	}
	return 0;
}
"""


@pytest.mark.skipif(COMPILER is None, reason="needs a C compiler")
def test_the_c_walker_answers_what_the_c_registry_answers(
		tmp_path: Path) -> None:
	import subprocess

	walker = ROOT / "walker" / "c"
	schema = parse(Source("settings.situ", SCHEMA))
	blob   = pack(schema, resolve(schema, solve(schema)), metadata=True)[0]
	(tmp_path / "image.bin").write_bytes(blob)
	(tmp_path / "probe.c").write_text(C_PROBE, encoding="ascii")
	binary = tmp_path / "probe"
	assert COMPILER is not None		# the skip above says so
	built  = subprocess.run(
		[COMPILER, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
		 f"-I{walker}", str(tmp_path / "probe.c"),
		 str(walker / "situ_walk.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	argv = [str(binary), str(tmp_path / "image.bin")]
	for op, key, arg, _ in CASES:
		argv += [op, key, arg]
	said = subprocess.run(argv, capture_output=True, text=True,
	                      check=True).stdout.splitlines()
	want = [("found" if expected != "-" else "-") if op == "find"
	        else expected for op, _, _, expected in CASES]
	assert said == want, [(case, got) for case, got, expect
	                      in zip(CASES, said, want) if got != expect]
