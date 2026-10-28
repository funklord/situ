/* situ-walk-c -- read a message against a packed image.
 *
 * The embedded walker as a program rather than a library with a test
 * harness. What a device links is `situ_walk.c`; this is what a person runs
 * to see that it works, and what the differential test could drive if it
 * preferred a binary to a compile step.
 *
 * Deliberately thin. Everything it knows is in `situ_walk.h`, and a feature
 * that lives here rather than there is a feature a device cannot have.
 */
#include <stdio.h>
#include <stdlib.h>

#include "situ_walk.h"

#define IMAGE_MAX   (1u << 20)
#define MESSAGE_MAX 4096u
/* More arguments than any schema has a use for. A ceiling rather than an
 * allocation, this program having none. */
#define ARGS_MAX    16u

static uint8_t image_bytes[IMAGE_MAX];
static uint8_t message_bytes[MESSAGE_MAX];

static const char *why(situ_walk_err err)
{
	switch (err) {
	case SITU_WALK_OK:          return "ok";
	case SITU_WALK_BOUNDS:      return "bounds";
	case SITU_WALK_CONSTRAINT:  return "constraint";
	case SITU_WALK_MALFORMED:   return "malformed";
	case SITU_WALK_UNSUPPORTED: return "this build does not render it";
	case SITU_WALK_ARGUMENT:    return "an argument nobody supplied";
	default:                    return "unknown";
	}
}

/* What a hex digit is worth, or that it is not one. `strtoul` was reading
 * the pair and cannot tell `zz` from `00`, which is the third of the three
 * substitutions below. */
static int hex_digit(char c, unsigned int *value)
{
	if (c >= '0' && c <= '9') {
		*value = (unsigned int)(c - '0');
		return 1;
	}
	if (c >= 'a' && c <= 'f') {
		*value = (unsigned int)(c - 'a') + 10u;
		return 1;
	}
	if (c >= 'A' && c <= 'F') {
		*value = (unsigned int)(c - 'A') + 10u;
		return 1;
	}
	return 0;
}

#define HEX_OK       0
#define HEX_DIGITS   1
#define HEX_TOO_LONG 2

/* Hex on the command line, so the program needs no file format of its own
 * and a caller can paste a capture straight in.
 *
 * The whole of it or none of it. Three ways this quietly walked a DIFFERENT
 * message from the one it was handed -- a capture past the cap, an odd
 * digit count, and a digit that is not hex -- and `bin/situ-walk` refuses
 * all three, `bytes.fromhex` raising on the last two. Two readers of one
 * image answering about two different frames is the one thing this program
 * exists to make impossible: measured at 5000 bytes, where the Python tool
 * reads a `length` of 5000 that fits its frame and this one saw the same
 * 5000 in a frame of 4096 (26.719).
 *
 * A ceiling and a refusal rather than an allocation, this program still
 * having none. */
static int from_hex(const char *text, uint8_t *out, uint32_t cap,
                    uint32_t *len)
{
	uint32_t n = 0u;

	for (; text[0] != '\0'; text += 2) {
		unsigned int high = 0u;
		unsigned int low  = 0u;

		if (text[1] == '\0' || !hex_digit(text[0], &high)
		                || !hex_digit(text[1], &low)) {
			return HEX_DIGITS;
		}
		if (n >= cap) {
			return HEX_TOO_LONG;
		}
		out[n] = (uint8_t)((high << 4) | low);
		n++;
	}
	*len = n;
	return HEX_OK;
}

int main(int argc, char **argv)
{
	if (argc < 3) {
		fprintf(stderr, "usage: situ-walk-c <image> <hex> [struct] "
		                "[arg...]\n");
		return 2;
	}

	FILE *held = fopen(argv[1], "rb");
	if (held == NULL) {
		fprintf(stderr, "situ-walk-c: cannot open %s\n", argv[1]);
		return 2;
	}
	const size_t got = fread(image_bytes, 1u, sizeof image_bytes, held);
	fclose(held);

	situ_walk_image image;
	situ_walk_err err = situ_walk_open(&image, image_bytes, (uint32_t)got);
	if (err != SITU_WALK_OK) {
		fprintf(stderr, "situ-walk-c: %s\n", why(err));
		return 1;
	}

	uint32_t len = 0u;

	switch (from_hex(argv[2], message_bytes, MESSAGE_MAX, &len)) {
	case HEX_DIGITS:
		fprintf(stderr, "situ-walk-c: the message is not a run of hex "
		        "digit pairs\n");
		return 1;
	case HEX_TOO_LONG:
		fprintf(stderr, "situ-walk-c: the message is longer than the %u "
		        "bytes this build reads\n", MESSAGE_MAX);
		return 1;
	default:
		break;
	}

	const uint32_t shape = (argc > 3) ? (uint32_t)strtoul(argv[3], NULL, 10)
	                                  : 0u;

	/* The arguments the struct's `parameter` members take (decision 0050),
	 * positionally and in declaration order -- the same order and spelling
	 * the library's own `situ_walk_acquire` takes.
	 *
	 * Always called, even where there are none, because that is the case
	 * worth getting a message for: a schema that takes an argument and a
	 * caller that gave none is a walk that would read the wrong bytes, and
	 * `situ_walk_acquire` is what says so before anything is read. */
	static int64_t args[ARGS_MAX];
	uint32_t       supplied = 0u;
	for (int i = 4; i < argc && supplied < ARGS_MAX; i++) {
		args[supplied] = (int64_t)strtoll(argv[i], NULL, 0);
		supplied++;
	}
	err = situ_walk_acquire(&image, shape, args, supplied);
	if (err != SITU_WALK_OK) {
		fprintf(stderr, "situ-walk-c: struct %u: %s\n", shape, why(err));
		return 1;
	}

	uint32_t first = 0u;
	uint32_t count = 0u;
	err = situ_walk_members(&image, shape, &first, &count);
	if (err != SITU_WALK_OK) {
		fprintf(stderr, "situ-walk-c: struct %u: %s\n", shape, why(err));
		return 1;
	}

	printf("struct %u: %u members over %u bytes\n", shape, count, len);
	for (uint32_t i = 0u; i < count; i++) {
		uint64_t value  = 0u;
		uint32_t offset = 0u;
		uint32_t bits   = 0u;
		const situ_walk_err at = situ_walk_offset_bits(&image, message_bytes,
		                                               len, shape, first + i,
		                                               &offset);
		/* The width is printed beside the value because a member this build
		 * declines to *value* still has an extent, and the extent is what
		 * places everything after it. A delimited member is the case: no
		 * number to give, and four bytes wide over `GET `. */
		const situ_walk_err wide = situ_walk_size_bits(&image, message_bytes,
		                                               len, shape, first + i,
		                                               &bits);
		err = situ_walk_read(&image, message_bytes, len, shape, first + i,
		                     &value);

		if (at != SITU_WALK_OK) {
			printf("  [%u] -- %s\n", i, why(at));
		} else if (err == SITU_WALK_OK) {
			printf("  [%u] @%u +%u = %llu\n", i, offset / 8u, bits / 8u,
			       (unsigned long long)value);
		} else if (wide == SITU_WALK_OK) {
			printf("  [%u] @%u +%u -- %s\n", i, offset / 8u, bits / 8u,
			       why(err));
		} else {
			printf("  [%u] @%u -- %s\n", i, offset / 8u, why(err));
		}
	}

	return 0;
}
