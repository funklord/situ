/* walker_c_differ.c -- the C walker's verdict against the generated C
 * reader's, coverage-guided over both (26.1152).
 *
 * `walker_c_differ.py` writes three files beside this one and builds it:
 * `differ_driver.c`, the four-way differential's C driver with its `main`
 * renamed, `walk_image.h`, the schema's packed image, and `walk_shapes.h`,
 * each struct's name and its index in that image. Per input, the driver
 * runs with its standard output captured in memory, and every struct whose
 * section prints a `validate` line is asked of `situ_walk_validate` too;
 * a difference aborts, and libFuzzer saves the input.
 *
 * Three answers are not compared, each for a reason the Python twin
 * (`walker_differ_fuzz.py`) gives: a struct the reader cannot view (no
 * `validate` line), one the walker cannot say about (an argument, a
 * construct this build declines), and a section whose sealed region both
 * say they cannot open (26.1142).
 *
 * Bounded: an input over 1024 bytes is skipped, and libFuzzer's
 * `-max_total_time` ends the run.
 */
#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define main situ_differ_main
#include "differ_driver.c"
#undef main

#include "situ_walk.h"
#include "walk_image.h"
#include "walk_shapes.h"

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

static situ_walk_image image;
static int             ready;

/* The verdict a section of the driver's output states, or -1 where it
 * states none or says a sealed region was not opened. */
static int stated(const char *out, const char *name)
{
	char        head[256];
	const char *at;
	const char *end;
	const char *line;

	snprintf(head, sizeof head, "-- %s\n", name);
	at = strstr(out, head);
	if (at == NULL) {
		return -1;
	}
	at += strlen(head);
	end = strstr(at, "\n-- ");
	if (end == NULL) {
		end = at + strlen(at);
	}
	for (line = at; line < end; ) {
		const char *next = memchr(line, '\n', (size_t)(end - line));
		const size_t len = next == NULL ? (size_t)(end - line)
		                                : (size_t)(next - line);

		if (len > 0 && memmem(line, len, " refused=1 ", 11) != NULL) {
			return -1;
		}
		if (len > 9 && memcmp(line, "validate ", 9) == 0) {
			return atoi(line + 9);
		}
		line = next == NULL ? end : next + 1;
	}
	return -1;
}

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
	static const char digits[] = "0123456789abcdef";
	char   *hex;
	char   *out = NULL;
	size_t  out_len = 0;
	FILE   *captured;
	FILE   *saved;
	char   *argv[3];

	if (!ready) {
		if (situ_walk_open(&image, IMAGE_BLOB,
		                   (uint32_t)sizeof IMAGE_BLOB) != SITU_WALK_OK) {
			abort();
		}
		ready = 1;
	}
	if (size > 1024u) {
		return 0;
	}

	hex = malloc(size * 2u + 1u);
	if (hex == NULL) {
		return 0;
	}
	for (size_t i = 0; i < size; i++) {
		hex[2 * i]     = digits[data[i] >> 4];
		hex[2 * i + 1] = digits[data[i] & 15u];
	}
	hex[size * 2u] = '\0';

	captured = open_memstream(&out, &out_len);
	if (captured == NULL) {
		free(hex);
		return 0;
	}
	saved  = stdout;
	stdout = captured;
	argv[0] = "driver";
	argv[1] = hex;
	argv[2] = NULL;
	(void)situ_differ_main(2, argv);
	fflush(captured);
	stdout = saved;
	fclose(captured);
	free(hex);

	for (size_t s = 0; s < sizeof SHAPES / sizeof SHAPES[0]; s++) {
		situ_walk_err verdict = SITU_WALK_OK;
		const int     reader  = stated(out, SHAPES[s].name);
		int           walker;

		if (reader < 0
		    || situ_walk_acquire(&image, SHAPES[s].shape, NULL, 0u)
		       != SITU_WALK_OK
		    || situ_walk_validate(&image, data, (uint32_t)size,
		                          SHAPES[s].shape, &verdict)
		       != SITU_WALK_OK) {
			continue;
		}
		walker = verdict == SITU_WALK_OK ? 0
		         : verdict == SITU_WALK_BOUNDS ? 1
		         : verdict == SITU_WALK_VERSION ? 3 : 2;
		if (walker != reader) {
			fprintf(stderr, "walker_c_differ: %s: the reader says %d, the "
			        "walker %d\n", SHAPES[s].name, reader, walker);
			abort();
		}
	}
	free(out);
	return 0;
}
