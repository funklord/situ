"""A registry generates a table and four functions in C (0070, step 3).

fuzznet's settings, in the shape `state/schema.h` holds them: a key -- or a
family's instance -- found in the table, a cell checked by its type's own
`validate` and then by the entry's bounds, its value read through the
type's getter, and of two values the safer one by the entry's direction.
The cell types are the ones 26.1075 to 26.1079 made writable: a count, a
flag spelled `on`/`off`/`1`/`0`, a duration `14d` bounded in seconds, a
bounded ASCII text, and `4+2`.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest							# noqa: E402

from situc.cli import analyse					# noqa: E402
from situc.codegen.c import generate				# noqa: E402
from situc.diagnostics import Source				# noqa: E402
from situc.parser import parse					# noqa: E402

RUNTIME  = ROOT / "runtime" / "c"
HOST_CC  = shutil.which("gcc") or shutil.which("cc")
WARNINGS = ["-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
            "-Wconversion", "-Wsign-conversion"]

SCHEMA = """target buffer;
endian big;

policy scope   { estate, host }
policy display { restart, sensitive, advanced, verb, advertised }

tokens flag_word { on = "on", off = "off", one = "1", zero = "0", default = error, }
tokens time_unit { d = "d", h = "h", m = "m", s = "s", default = error, }

struct count {
	decimal u64  value[remaining]  [minimal];
}

struct flag {
	flag_word  word[remaining];
	let u8 value = (word == flag_word.on) | (word == flag_word.one);
}

struct duration {
	decimal u32  count[remaining]  [minimal];
	time_unit    unit[1];
	let u64 value = count * ((unit == time_unit.d) * 86400
	                       + (unit == time_unit.h) * 3600
	                       + (unit == time_unit.m) * 60
	                       + (unit == time_unit.s));
}

struct text {
	u8  bytes[remaining]  [encoding = ascii];
}

struct erasure {
	decimal u8  data until "+" max 3;
	decimal u8  parity[remaining];
}

registry node {
	"revocation/k"    : count [min = 1, max = 255, scope = estate,
	                           safe = higher, unit = "root signatures"]
	                    "the estate's k";
	"routing/public"  : flag [default = "off", scope = estate, safe = lower,
	                          display = advertised];
	"archive/gone"    : duration [min = 1, max = 315360000, default = "14d",
	                              scope = estate, display = advanced];
	"host/name"       : text [min_size = 1, max_size = 8, scope = host];
	"archive/erasure" : erasure [default = "4+2", scope = estate];
	"stream/grant/"   : count [max = 3, scope = estate, display = verb];
}
"""

PROBE = r"""
#include <stdio.h>
#include <string.h>
#include "settings.h"

static const situ_node_entry_t *find(const char *key)
{
	return situ_node_find((const uint8_t *)key, (uint32_t)strlen(key));
}

static int check(const char *key, const char *value)
{
	return (int)situ_node_check(find(key), (const uint8_t *)value,
	                            (uint32_t)strlen(value));
}

static long long value(const char *key, const char *cell)
{
	int64_t out = -1;
	if (situ_node_value(find(key), (const uint8_t *)cell,
	                    (uint32_t)strlen(cell), &out) != SITU_OK)
		return -1;
	return (long long)out;
}

static int safer(const char *key, const char *a, const char *b)
{
	return situ_node_safer(find(key), (const uint8_t *)a, (uint32_t)strlen(a),
	                       (const uint8_t *)b, (uint32_t)strlen(b));
}

int main(int argc, char **argv)
{
	for (int i = 1; i + 2 < argc; i += 3) {
		const char *op = argv[i], *key = argv[i + 1], *arg = argv[i + 2];
		if (strcmp(op, "find") == 0) {
			const situ_node_entry_t *e = find(key);
			printf("%s\n", e ? e->key : "-");
		} else if (strcmp(op, "check") == 0) {
			printf("%d\n", check(key, arg));
		} else if (strcmp(op, "value") == 0) {
			printf("%lld\n", value(key, arg));
		} else {
			const char *comma = strchr(arg, ',');
			char first[64];
			memcpy(first, arg, (size_t)(comma - arg));
			first[comma - arg] = '\0';
			printf("%d\n", safer(key, first, comma + 1));
		}
	}
	const situ_node_entry_t *e = find("archive/gone");
	printf("row %s %s %u %u\n", e->def, e->unit ? e->unit : "-",
	       (unsigned)e->scope, (unsigned)e->display);
	return 0;
}
"""

#: (op, key, argument, what the probe prints)
CASES = [
	("find", "revocation/k", "", "revocation/k"),
	("find", "stream/grant/ab12", "", "stream/grant/"),
	("find", "stream/grant/", "", "-"),			# a family with no word
	("find", "stream/grant/AB", "", "-"),			# not a-z0-9
	("find", "stream/grant/a/b", "", "-"),			# two words
	("find", "revocation", "", "-"),
	("check", "revocation/k", "3", "0"),
	("check", "revocation/k", "0", "2"),			# below `min`
	("check", "revocation/k", "256", "2"),			# above `max`
	("check", "revocation/k", "03", "2"),			# not minimal
	("check", "revocation/k", "x", "2"),
	("value", "revocation/k", "42", "42"),
	("safer", "revocation/k", "3,5", "1"),			# higher stands
	("safer", "revocation/k", "5,3", "0"),
	("safer", "revocation/k", "5,5", "0"),
	("value", "routing/public", "on", "1"),
	("value", "routing/public", "0", "0"),
	("check", "routing/public", "yes", "2"),
	("safer", "routing/public", "on,off", "1"),		# lower stands
	("safer", "routing/public", "off,on", "0"),
	("value", "archive/gone", "14d", "1209600"),
	("value", "archive/gone", "2h", "7200"),
	("check", "archive/gone", "0s", "2"),			# below a second
	("check", "archive/gone", "3651d", "2"),		# past ten years
	("check", "archive/gone", "14w", "2"),
	("check", "host/name", "", "2"),			# below `min_size`
	("check", "host/name", "nabbe", "0"),
	("check", "host/name", "ninechars", "2"),		# above `max_size`
	("value", "host/name", "nabbe", "-1"),			# no value: its bytes
	("check", "archive/erasure", "4+2", "0"),
	("check", "archive/erasure", "4-2", "2"),
	("check", "stream/grant/x1", "3", "0"),
	("check", "stream/grant/x1", "4", "2"),
]


@pytest.mark.skipif(HOST_CC is None, reason="needs a C compiler")
def test_the_c_registry_finds_checks_reads_and_orders(tmp_path: Path) -> None:
	path = tmp_path / "settings.situ"
	path.write_text(SCHEMA, encoding="ascii")
	source, resolved, _ = analyse(path)
	made = generate(parse(source), resolved, "settings")
	(tmp_path / "settings.h").write_text(made.header, encoding="ascii")
	(tmp_path / "settings.c").write_text(made.source, encoding="ascii")
	(tmp_path / "probe.c").write_text(PROBE, encoding="ascii")
	binary = tmp_path / "probe"
	assert HOST_CC is not None		# the skip above says so
	built = subprocess.run(
		[HOST_CC, *WARNINGS, f"-I{RUNTIME}", f"-I{tmp_path}",
		 str(tmp_path / "probe.c"), str(tmp_path / "settings.c"),
		 str(RUNTIME / "situ.c"), "-o", str(binary)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	argv = [str(binary)]
	for op, key, arg, _ in CASES:
		argv += [op, key, arg]
	said = subprocess.run(argv, capture_output=True, text=True,
	                      check=True).stdout.splitlines()
	assert said[:-1] == [want for *_, want in CASES], [
		(case, got) for case, got in zip(CASES, said) if case[-1] != got]
	# The row: default and unit as written, and each policy as its bits --
	# `scope = estate` is bit 0, `display = advanced` bit 2.
	assert said[-1] == "row 14d - 1 4"


def test_a_cell_type_that_ends_short_of_its_bytes_is_refused(
		tmp_path: Path) -> None:
	"""A type neither one size nor run to its frame's end would leave bytes
	after it that nothing checks."""
	text = SCHEMA + """
struct short_one { u8 n; u8 d[n]; }
registry other { "a" : short_one; }
"""
	path = tmp_path / "short.situ"
	path.write_text(text, encoding="ascii")
	source, resolved, _ = analyse(path)
	with pytest.raises(Exception, match="would not end where its bytes do"):
		generate(parse(Source(str(path), text)), resolved, "short")
