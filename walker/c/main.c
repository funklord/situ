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
	default:                    return "unknown";
	}
}

/* Hex on the command line, so the program needs no file format of its own
 * and a caller can paste a capture straight in. */
static uint32_t from_hex(const char *text, uint8_t *out, uint32_t cap)
{
	uint32_t len = 0u;
	while (text[0] != '\0' && text[1] != '\0' && len < cap) {
		char pair[3];
		pair[0] = text[0];
		pair[1] = text[1];
		pair[2] = '\0';
		out[len] = (uint8_t)strtoul(pair, NULL, 16);
		len++;
		text += 2;
	}
	return len;
}

int main(int argc, char **argv)
{
	if (argc < 3) {
		fprintf(stderr, "usage: situ-walk-c <image> <hex> [struct]\n");
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

	const uint32_t len   = from_hex(argv[2], message_bytes, MESSAGE_MAX);
	const uint32_t shape = (argc > 3) ? (uint32_t)strtoul(argv[3], NULL, 10)
	                                  : 0u;

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
		const situ_walk_err at = situ_walk_offset_bits(&image, message_bytes,
		                                               len, shape, first + i,
		                                               &offset);
		err = situ_walk_read(&image, message_bytes, len, shape, first + i,
		                     &value);

		if (err == SITU_WALK_OK) {
			printf("  [%u] @%u = %llu\n", i, offset / 8u,
			       (unsigned long long)value);
		} else if (at == SITU_WALK_OK) {
			printf("  [%u] @%u -- %s\n", i, offset / 8u, why(err));
		} else {
			printf("  [%u] -- %s\n", i, why(err));
		}
	}

	return 0;
}
