/* test_bmp.c -- read a BMP that something else wrote (section 26.35).
 *
 * `examples/bmp` is the little-endian worked example and the misaligned one,
 * and until now the only thing that had read one was situ. Every check over it
 * was derived from the schema -- `gen-checks` asserts the offsets the schema
 * declares, the differential drivers ask four backends the same question about
 * random bytes -- and none of those can say whether the schema describes the
 * format. `examples/http` is what that costs: a cap off by one made every real
 * response unparseable, and nothing noticed for a year because nothing handed
 * it an HTTP message.
 *
 * The bytes below are two files ImageMagick wrote:
 *
 *   convert -size 3x2 xc:red   -type truecolor BMP3:red.bmp
 *   convert -size 1x1 xc:white -type truecolor BMP3:white.bmp
 *
 * and their headers are examples/bmp/bmp.vectors, which records how to
 * regenerate them and which `gen-tests` turns into the field-by-field suite
 * beside this one. What every field reads back as is that suite's; this one is
 * for the two things a per-struct vector cannot state, because they are about
 * a whole file:
 *
 *   - `pixel_offset` has to be the two struct sizes the `require size(...)`
 *     lines pin, and `file_size` has to be that plus `image_size`;
 *   - and `file_size` has to be the number of bytes there actually are.
 *
 * A header this schema misplaced by a byte would still read some number out of
 * every field. Those sums are what would stop adding up.
 */

#include <setjmp.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include <cmocka.h>

#include "situ.h"

#include "bmp.h"

/* 3x2 truecolour red: 54 bytes of header, then two 12-byte rows. */
static const uint8_t RED[] = {
	0x42, 0x4D,			/* "BM"				*/
	0x4E, 0x00, 0x00, 0x00,		/* file_size    = 78		*/
	0x00, 0x00, 0x00, 0x00,		/* two reserved u16, both zero	*/
	0x36, 0x00, 0x00, 0x00,		/* pixel_offset = 54		*/
	0x28, 0x00, 0x00, 0x00,		/* header_size  = 40		*/
	0x03, 0x00, 0x00, 0x00,		/* width        = 3		*/
	0x02, 0x00, 0x00, 0x00,		/* height       = 2		*/
	0x01, 0x00,			/* planes       = 1		*/
	0x18, 0x00,			/* bits_per_pixel = 24		*/
	0x00, 0x00, 0x00, 0x00,		/* compression  = rgb		*/
	0x18, 0x00, 0x00, 0x00,		/* image_size   = 24		*/
	0x00, 0x00, 0x00, 0x00,		/* x_pixels_per_meter		*/
	0x00, 0x00, 0x00, 0x00,		/* y_pixels_per_meter		*/
	0x00, 0x00, 0x00, 0x00,		/* colours_used			*/
	0x00, 0x00, 0x00, 0x00,		/* important_colours		*/
	0x00, 0x00, 0xFF, 0x00, 0x00, 0xFF, 0x00, 0x00, 0xFF, 0x00, 0x00, 0x00,
	0x00, 0x00, 0xFF, 0x00, 0x00, 0xFF, 0x00, 0x00, 0xFF, 0x00, 0x00, 0x00,
};

/* 1x1 truecolour white: one row of three bytes padded to four. */
static const uint8_t WHITE[] = {
	0x42, 0x4D,
	0x3A, 0x00, 0x00, 0x00,		/* file_size    = 58		*/
	0x00, 0x00, 0x00, 0x00,
	0x36, 0x00, 0x00, 0x00,		/* pixel_offset = 54		*/
	0x28, 0x00, 0x00, 0x00,
	0x01, 0x00, 0x00, 0x00,		/* width        = 1		*/
	0x01, 0x00, 0x00, 0x00,		/* height       = 1		*/
	0x01, 0x00,
	0x18, 0x00,
	0x00, 0x00, 0x00, 0x00,
	0x04, 0x00, 0x00, 0x00,		/* image_size   = 4		*/
	0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00,
	0xFF, 0xFF, 0xFF, 0x00,
};

struct opened {
	situ_msg_t   msg;
	situ_view_t  file;
	situ_view_t  info;
};

/* The two headers of one file. The second is at the first's own size, which
 * is the layout claim this file exists to check against something else's
 * bytes. */
static void open_file(struct opened *held, const uint8_t *data, uint32_t len)
{
	situ_msg_init(&held->msg, (uint8_t *)(uintptr_t)(const void *)data, len);

	assert_int_equal(situ_bitmap_file_header_view(&held->msg, 0, &held->file),
	                 SITU_OK);
	assert_int_equal(situ_bitmap_info_header_view(
		&held->msg, SITU_BITMAP_FILE_HEADER_SIZE_FIXED, &held->info),
	                 SITU_OK);
}

/* The one field the vector suite has no way to state: a marker is a byte
 * array, and a vector's expectations are values. It is also the field a reader
 * uses to decide whether this is a BMP at all. */
static void test_the_signature_is_bm(void **state)
{
	struct opened held;

	(void)state;

	open_file(&held, RED, (uint32_t)sizeof(RED));
	assert_memory_equal(situ_bitmap_file_header_signature_ptr(held.file),
	                    "BM", 2);

	open_file(&held, WHITE, (uint32_t)sizeof(WHITE));
	assert_memory_equal(situ_bitmap_file_header_signature_ptr(held.file),
	                    "BM", 2);
}

/* Both headers pass every constraint the schema declares -- the two reserved
 * halves are zero, the info header says 40, the plane count says 1. A real
 * file failing `validate` would mean the schema forbids what the format
 * permits, which is the other way for a description to be wrong. */
static void test_a_real_file_validates(void **state)
{
	struct opened held;

	(void)state;

	open_file(&held, RED, (uint32_t)sizeof(RED));
	assert_int_equal(situ_bitmap_file_header_validate(held.file), SITU_OK);
	assert_int_equal(situ_bitmap_info_header_validate(held.info), SITU_OK);

	open_file(&held, WHITE, (uint32_t)sizeof(WHITE));
	assert_int_equal(situ_bitmap_file_header_validate(held.file), SITU_OK);
	assert_int_equal(situ_bitmap_info_header_validate(held.info), SITU_OK);
}

/* The file's own arithmetic, which is where a misplaced field shows. The
 * pixels start after both headers and nothing else, so 14 + 40 has to be the
 * number the file wrote -- and the schema pins those two with
 * `require size(...)`. */
static void test_the_pixels_start_where_the_two_headers_end(void **state)
{
	struct opened held;

	(void)state;
	assert_int_equal(SITU_BITMAP_FILE_HEADER_SIZE_FIXED
	                 + SITU_BITMAP_INFO_HEADER_SIZE_FIXED, 54);

	open_file(&held, RED, (uint32_t)sizeof(RED));
	assert_int_equal(situ_bitmap_file_header_pixel_offset_get(held.file),
	                 SITU_BITMAP_FILE_HEADER_SIZE_FIXED
	                 + SITU_BITMAP_INFO_HEADER_SIZE_FIXED);

	open_file(&held, WHITE, (uint32_t)sizeof(WHITE));
	assert_int_equal(situ_bitmap_file_header_pixel_offset_get(held.file),
	                 SITU_BITMAP_FILE_HEADER_SIZE_FIXED
	                 + SITU_BITMAP_INFO_HEADER_SIZE_FIXED);
}

static void test_the_file_size_is_the_headers_plus_the_pixels(void **state)
{
	static const struct {
		const uint8_t  *data;
		uint32_t        len;
	} FILES[] = {
		{ RED,   (uint32_t)sizeof(RED)   },
		{ WHITE, (uint32_t)sizeof(WHITE) },
	};
	size_t i;

	(void)state;

	for (i = 0; i < sizeof(FILES) / sizeof(FILES[0]); i++) {
		struct opened held;

		open_file(&held, FILES[i].data, FILES[i].len);

		/* What the file says about itself... */
		assert_int_equal(situ_bitmap_file_header_file_size_get(held.file),
		                 situ_bitmap_file_header_pixel_offset_get(held.file)
		                 + situ_bitmap_info_header_image_size_get(held.info));
		/* ...and what it actually is. */
		assert_int_equal(situ_bitmap_file_header_file_size_get(held.file),
		                 FILES[i].len);
	}
}

/* A row is padded to a multiple of four bytes, which is the one rule of the
 * pixel array the headers alone can be held to: 3 pixels x 3 bytes is 9,
 * padded to 12, times 2 rows is the 24 the file declares. The schema stops
 * before the pixels, and this is the arithmetic that says its `image_size` is
 * the field it thinks it is. */
static void test_the_image_size_is_the_padded_rows(void **state)
{
	struct opened held;
	uint32_t row;

	(void)state;
	open_file(&held, RED, (uint32_t)sizeof(RED));

	row = ((uint32_t)situ_bitmap_info_header_width_get(held.info)
	       * situ_bitmap_info_header_bits_per_pixel_get(held.info) / 8u
	       + 3u) & ~3u;

	assert_int_equal(row, 12);
	assert_int_equal(row * (uint32_t)situ_bitmap_info_header_height_get(held.info),
	                 situ_bitmap_info_header_image_size_get(held.info));
}

/* The V4 header ImageMagick writes without `BMP3:` is 108 bytes, and is what
 * this schema is not about. `must_eq = 40` is the whole of the distinction,
 * and a reader who took the first four bytes of it as a size would find 108
 * where the schema promises 40. */
static void test_a_later_header_version_is_refused(void **state)
{
	uint8_t copy[sizeof(RED)];
	struct opened held;

	(void)state;
	memcpy(copy, RED, sizeof(RED));
	copy[14] = 108;

	open_file(&held, copy, (uint32_t)sizeof(copy));
	assert_int_equal(situ_bitmap_info_header_validate(held.info),
	                 SITU_ERR_CONSTRAINT);
}

int main(void)
{
	const struct CMUnitTest tests[] = {
		cmocka_unit_test(test_the_signature_is_bm),
		cmocka_unit_test(test_a_real_file_validates),
		cmocka_unit_test(test_the_pixels_start_where_the_two_headers_end),
		cmocka_unit_test(test_the_file_size_is_the_headers_plus_the_pixels),
		cmocka_unit_test(test_the_image_size_is_the_padded_rows),
		cmocka_unit_test(test_a_later_header_version_is_refused),
	};

	return cmocka_run_group_tests(tests, NULL, NULL);
}
