"""The check prelude and runner every generated C suite carries (26.977).

`gen-checks`, `gen-tests` and `gen-codec-tests` emitted cmocka suites, so a
user handed one had to install a test framework before they could run a
file situ wrote for them -- and the cross targets could not run them at all,
cmocka having no aarch64 build here (`test/cross/probe.c` became a
self-checking program for that reason). The C++ suites have always been a
plain `main` that counts its own failures; this gives the C ones the same,
in about sixty lines of C that sit in the file itself.

What was used of cmocka was assertions, a list of tests and a runner, so
that is what this is. A failed check prints where and what -- the two
values for an equality, the first differing byte for a byte compare -- and
leaves the test by `longjmp`, as cmocka does, so the next test still runs.
The runner prints the lines cmocka printed (`[ RUN      ]`, `[       OK ]`,
`[  FAILED  ]`, `[  PASSED  ]`), which logs and readers already key on, and
exits non-zero where anything failed.

Everything is `static inline` or a macro, so a suite that uses half of it
compiles under `-Wall -Wextra -Werror` without an unused-function warning.
"""

from __future__ import annotations

#: The headers the prelude needs, in place of cmocka's four.
INCLUDES = [
	"#include <inttypes.h>",
	"#include <setjmp.h>",
	"#include <stddef.h>",
	"#include <stdint.h>",
	"#include <stdio.h>",
	"#include <string.h>",
]

PRELUDE = r"""
/* Checks and a runner, in the file rather than a framework: what this
 * suite needs to run is a C compiler and the situ runtime. A failed check
 * says where and what, and leaves its test; the next test runs. */
static jmp_buf situ_check_jump;

static inline void situ_check_fail(const char *file, int line,
		const char *what)
{
	printf("%s:%d: check failed: %s\n", file, line, what);
	longjmp(situ_check_jump, 1);
}

static inline void situ_check_eq_at(uintmax_t got, uintmax_t want,
		int equal, const char *what, const char *file, int line)
{
	if ((got == want) != equal) {
		printf("%s:%d: 0x%" PRIxMAX " %s 0x%" PRIxMAX "\n", file, line,
		       got, equal ? "!=" : "==", want);
		situ_check_fail(file, line, what);
	}
}

static inline void situ_check_bytes_at(const void *got, const void *want,
		size_t count, const char *what, const char *file, int line)
{
	const unsigned char *a = (const unsigned char *)got;
	const unsigned char *b = (const unsigned char *)want;
	size_t i;

	for (i = 0; i < count; i++) {
		if (a[i] != b[i]) {
			printf("%s:%d: byte %zu of %zu is 0x%02x, not 0x%02x\n",
			       file, line, i, count, a[i], b[i]);
			situ_check_fail(file, line, what);
		}
	}
}

#define situ_check_eq(got, want) \
	situ_check_eq_at((uintmax_t)(got), (uintmax_t)(want), 1, \
	                 #got " == " #want, __FILE__, __LINE__)
#define situ_check_ne(got, want) \
	situ_check_eq_at((uintmax_t)(got), (uintmax_t)(want), 0, \
	                 #got " != " #want, __FILE__, __LINE__)
#define situ_check_true(cond) \
	do { if (!(cond)) situ_check_fail(__FILE__, __LINE__, #cond); } while (0)
#define situ_check_false(cond) \
	do { if (cond) situ_check_fail(__FILE__, __LINE__, "!(" #cond ")"); } while (0)
#define situ_check_null(ptr) situ_check_true((ptr) == NULL)
#define situ_check_non_null(ptr) situ_check_true((ptr) != NULL)
#define situ_check_bytes(got, want, count) \
	situ_check_bytes_at((got), (want), (size_t)(count), \
	                    #got " == " #want, __FILE__, __LINE__)
#define situ_check_bytes_ne(got, want, count) \
	situ_check_true(memcmp((got), (want), (size_t)(count)) != 0)
#define situ_check_ptr_eq(got, want) \
	situ_check_true((const void *)(got) == (const void *)(want))
#define situ_check_str_eq(got, want) \
	situ_check_true(strcmp((got), (want)) == 0)

struct situ_check_case {
	const char *name;
	void (*run)(void **state);
};

#define situ_check_case(fn) { #fn, fn }

static inline int situ_check_run(const struct situ_check_case *cases,
		size_t count, const char *group)
{
	volatile size_t failed = 0;
	volatile size_t i;

	printf("[==========] %s: Running %zu test(s).\n", group, count);
	for (i = 0; i < count; i++) {
		printf("[ RUN      ] %s\n", cases[i].name);
		if (setjmp(situ_check_jump) == 0) {
			cases[i].run(NULL);
			printf("[       OK ] %s\n", cases[i].name);
		} else {
			failed++;
			printf("[  FAILED  ] %s\n", cases[i].name);
		}
	}
	printf("[==========] %s: %zu test(s) run.\n", group, count);
	if (failed == 0) {
		printf("[  PASSED  ] %zu test(s).\n", count);
		return 0;
	}
	printf("[  FAILED  ] %zu test(s).\n", (size_t)failed);
	return 1;
}
""".strip("\n").split("\n")


def header() -> str:
	"""The same, as a header for the hand-written suites in `test/generated`,
	which the Makefile writes from here so that there is one copy."""
	return "\n".join(["/* Generated from situc/codegen/c/selfcheck.py -- do not edit. */",
	                  "#ifndef SITU_CHECK_H", "#define SITU_CHECK_H", "",
	                  *INCLUDES, "", *PRELUDE, "", "#endif", ""])


def main_lines(array: str, names: list[str], group: str) -> list[str]:
	"""`int main` running `names` as group `group`."""
	return [
		"int main(void)",
		"{",
		f"\tconst struct situ_check_case {array}[] = {{",
		*(f"\t\tsitu_check_case({name})," for name in names),
		"\t};",
		"",
		f"\treturn situ_check_run({array}, sizeof {array} / sizeof {array}[0],"
		f" \"{group}\");",
		"}",
	]
