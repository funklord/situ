/* test_sqlite.c -- walk a real SQLite b-tree page (section 9.3).
 *
 * The worked example for `indexed`, and the other half of it: the schema says
 * a page holds a table of two-byte offsets measured from the start of the
 * page, and this checks that claim against a page sqlite3 wrote.
 *
 * The bytes below are page 2 of a database built by
 *
 *   sqlite3 t.db "PRAGMA page_size=512;
 *                 CREATE TABLE t(a TEXT);
 *                 INSERT INTO t VALUES('alpha'),('beta'),('gamma');"
 *
 * and are reproduced from examples/sqlite/vectors.txt, which records how to
 * regenerate them. A description that agrees only with its own compiler has
 * demonstrated nothing.
 */

#include <setjmp.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include <cmocka.h>

#include "situ.h"

#include "sqlite.h"

#define PAGE_SIZE	512u

/* Page 2, byte for byte. Everything between the pointer array and the cells is
 * the free space `content_start` names, and is zero. */
static void build_page(uint8_t page[PAGE_SIZE])
{
	static const uint8_t HEAD[] = {
		0x0D,			/* page_type       = 13, leaf table	*/
		0x00, 0x00,		/* first_freeblock = 0			*/
		0x00, 0x03,		/* cell_count      = 3			*/
		0x01, 0xE6,		/* content_start   = 486		*/
		0x00,			/* fragmented_free = 0			*/
		0x01, 0xF7,		/* cells[0] -> 503			*/
		0x01, 0xEF,		/* cells[1] -> 495			*/
		0x01, 0xE6,		/* cells[2] -> 486			*/
	};
	/* The three cells, from byte 486 to the end of the page. */
	static const uint8_t CELLS[] = {
		0x07, 0x03, 0x02, 0x17, 'g', 'a', 'm', 'm', 'a',
		0x06, 0x02, 0x02, 0x15, 'b', 'e', 't', 'a',
		0x07, 0x01, 0x02, 0x17, 'a', 'l', 'p', 'h', 'a',
	};

	memset(page, 0, PAGE_SIZE);
	memcpy(page, HEAD, sizeof(HEAD));
	memcpy(page + 486, CELLS, sizeof(CELLS));
}

static situ_view_t page_view(situ_msg_t *msg, uint8_t *page)
{
	situ_view_t view;

	build_page(page);
	situ_msg_init(msg, page, PAGE_SIZE);
	assert_int_equal(situ_btree_leaf_page_view(msg, 0, PAGE_SIZE, &view),
	                 SITU_OK);
	return view;
}

/* -- the header ------------------------------------------------------------ */

static void test_the_header_reads_as_sqlite_wrote_it(void **state)
{
	uint8_t page[PAGE_SIZE];
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	view = page_view(&msg, page);

	assert_int_equal(situ_btree_leaf_page_page_type_get(view), 13);
	assert_int_equal(situ_btree_leaf_page_first_freeblock_get(view), 0);
	assert_int_equal(situ_btree_leaf_page_cell_count_get(view), 3);
	assert_int_equal(situ_btree_leaf_page_content_start_get(view), 486);
	assert_int_equal(situ_btree_leaf_page_fragmented_free_get(view), 0);
}

static void test_a_page_that_is_not_a_leaf_table_fails_validate(void **state)
{
	uint8_t page[PAGE_SIZE];
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	view = page_view(&msg, page);
	assert_int_equal(situ_btree_leaf_page_validate(view), SITU_OK);

	/* 5 is an interior table page, which carries a twelfth header byte. */
	page[0] = 0x05;
	assert_int_equal(situ_btree_leaf_page_validate(view), SITU_ERR_CONSTRAINT);
}

/* -- the cell pointer array ------------------------------------------------ */

static void test_the_count_is_the_header_field(void **state)
{
	uint8_t page[PAGE_SIZE];
	situ_msg_t msg;
	situ_view_t view;

	(void)state;
	view = page_view(&msg, page);

	assert_int_equal(situ_btree_leaf_page_cells_count(view), 3);
}

static void test_every_offset_is_the_one_sqlite_wrote(void **state)
{
	uint8_t page[PAGE_SIZE];
	situ_msg_t msg;
	situ_view_t view;
	uint32_t found = 0;

	(void)state;
	view = page_view(&msg, page);

	assert_int_equal(situ_btree_leaf_page_cells_offset(view, 0, &found), SITU_OK);
	assert_int_equal(found, 503);
	assert_int_equal(situ_btree_leaf_page_cells_offset(view, 1, &found), SITU_OK);
	assert_int_equal(found, 495);
	assert_int_equal(situ_btree_leaf_page_cells_offset(view, 2, &found), SITU_OK);
	assert_int_equal(found, 486);
}

static void test_the_pointers_descend_while_the_keys_ascend(void **state)
{
	uint8_t page[PAGE_SIZE];
	situ_msg_t msg;
	situ_view_t view;
	uint32_t i, previous = PAGE_SIZE, found = 0;

	(void)state;
	view = page_view(&msg, page);

	/* The whole reason the format keeps a table rather than an array: the
	 * pointers are in key order and the cells fill the page backwards, so
	 * element N is nowhere an array could have put it. */
	for (i = 0; i < situ_btree_leaf_page_cells_count(view); i++) {
		assert_int_equal(situ_btree_leaf_page_cells_offset(view, i, &found),
		                 SITU_OK);
		assert_true(found < previous);
		previous = found;
	}
}

static void test_an_offset_is_measured_from_the_start_of_the_page(void **state)
{
	uint8_t page[PAGE_SIZE];
	situ_msg_t msg;
	situ_view_t view;
	uint32_t found = 0;

	(void)state;
	view = page_view(&msg, page);

	/* `base = page_type`, which is byte zero. The first cell's payload size
	 * varint sits exactly there, so the offset is an index into the page and
	 * not into the region after the header. */
	assert_int_equal(situ_btree_leaf_page_cells_offset(view, 2, &found), SITU_OK);
	assert_int_equal(found, situ_btree_leaf_page_content_start_get(view));
	assert_int_equal(view.base[found], 0x07);	/* payload_size of "gamma" */
}

static void test_an_entry_past_the_end_is_refused(void **state)
{
	uint8_t page[PAGE_SIZE];
	situ_msg_t msg;
	situ_view_t view;
	uint32_t found = 0;

	(void)state;
	view = page_view(&msg, page);

	assert_int_equal(situ_btree_leaf_page_cells_offset(view, 3, &found),
	                 SITU_ERR_BOUNDS);
}

static void test_a_count_larger_than_the_page_is_refused(void **state)
{
	uint8_t page[PAGE_SIZE];
	situ_msg_t msg;
	situ_view_t view;
	uint32_t found = 0;

	(void)state;
	view = page_view(&msg, page);

	/* A page claiming more cells than the table can hold: the count comes
	 * from the data, so it is checked against the buffer and not trusted. */
	page[3] = 0xFF;
	page[4] = 0xFF;
	assert_int_equal(situ_btree_leaf_page_cells_count(view), 0xFFFFu);
	assert_int_equal(situ_btree_leaf_page_cells_offset(view, 0xFFFEu, &found),
	                 SITU_ERR_BOUNDS);
}

/* -- the cells the table reaches ------------------------------------------- */

static void test_each_cell_decodes(void **state)
{
	uint8_t page[PAGE_SIZE];
	situ_msg_t msg;
	situ_view_t view, cell;
	uint64_t size = 0, rowid = 0;

	(void)state;
	view = page_view(&msg, page);

	/* The pointers are in key order, so cell N carries rowid N+1 -- while the
	 * cells themselves sit in the page back to front. */
	assert_int_equal(situ_btree_leaf_page_cells_at(view, 0, &cell), SITU_OK);
	assert_int_equal(situ_table_leaf_cell_payload_size_get(cell, &size), SITU_OK);
	assert_int_equal(situ_table_leaf_cell_rowid_get(cell, &rowid), SITU_OK);
	assert_int_equal(size, 7);
	assert_int_equal(rowid, 1);
	assert_memory_equal(situ_table_leaf_cell_payload_ptr(cell) + 2, "alpha", 5);

	assert_int_equal(situ_btree_leaf_page_cells_at(view, 1, &cell), SITU_OK);
	assert_int_equal(situ_table_leaf_cell_rowid_get(cell, &rowid), SITU_OK);
	assert_int_equal(rowid, 2);
	assert_memory_equal(situ_table_leaf_cell_payload_ptr(cell) + 2, "beta", 4);

	assert_int_equal(situ_btree_leaf_page_cells_at(view, 2, &cell), SITU_OK);
	assert_int_equal(situ_table_leaf_cell_rowid_get(cell, &rowid), SITU_OK);
	assert_int_equal(rowid, 3);
	assert_memory_equal(situ_table_leaf_cell_payload_ptr(cell) + 2, "gamma", 5);
}

static void test_a_cell_is_narrowed_to_its_own_extent(void **state)
{
	uint8_t page[PAGE_SIZE];
	situ_msg_t msg;
	situ_view_t view, cell;

	(void)state;
	view = page_view(&msg, page);

	/* Not to the rest of the page: the two varints plus the payload, which is
	 * what `_extent` computes from the cell's own bytes. */
	assert_int_equal(situ_btree_leaf_page_cells_at(view, 0, &cell), SITU_OK);
	assert_int_equal(cell.limit, 9);	/* 1 + 1 + 7 */

	assert_int_equal(situ_btree_leaf_page_cells_at(view, 1, &cell), SITU_OK);
	assert_int_equal(cell.limit, 8);	/* 1 + 1 + 6 */
}

/* -- the nine-byte varint, which is what makes this its own encoding -------- */

static void test_a_nine_byte_rowid_decodes(void **state)
{
	/* Cells sqlite3 wrote for rowids 2^56-1 and 2^60-1. The first is the
	 * longest eight-byte form; the second needs the ninth byte, whose eight
	 * bits and absent continuation flag distinguish this encoding from every
	 * other base-128. Regenerate with:
	 *
	 *   sqlite3 t.db "CREATE TABLE t(a);
	 *                 INSERT INTO t(rowid,a) VALUES(1152921504606846975,'y');"
	 */
	static const uint8_t EIGHT[] = {
		0x03, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x7F, 0x02, 0x0F, 'x',
	};
	static const uint8_t NINE[] = {
		0x03, 0x87, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
		0x02, 0x0F, 'y',
	};
	situ_msg_t msg;
	situ_view_t view;
	uint8_t scratch[16];
	uint64_t rowid = 0;

	(void)state;

	memcpy(scratch, EIGHT, sizeof(EIGHT));
	situ_msg_init(&msg, scratch, sizeof(EIGHT));
	assert_int_equal(situ_table_leaf_cell_view(&msg, 0, sizeof(EIGHT), &view),
	                 SITU_OK);
	assert_int_equal(situ_table_leaf_cell_rowid_get(view, &rowid), SITU_OK);
	assert_int_equal(situ_table_leaf_cell_rowid_len(view), 8);
	assert_true(rowid == 72057594037927935ULL);

	memcpy(scratch, NINE, sizeof(NINE));
	situ_msg_init(&msg, scratch, sizeof(NINE));
	assert_int_equal(situ_table_leaf_cell_view(&msg, 0, sizeof(NINE), &view),
	                 SITU_OK);
	assert_int_equal(situ_table_leaf_cell_rowid_get(view, &rowid), SITU_OK);
	assert_int_equal(situ_table_leaf_cell_rowid_len(view), 9);
	assert_true(rowid == 1152921504606846975ULL);
}

int main(void)
{
	const struct CMUnitTest tests[] = {
		cmocka_unit_test(test_the_header_reads_as_sqlite_wrote_it),
		cmocka_unit_test(test_a_page_that_is_not_a_leaf_table_fails_validate),
		cmocka_unit_test(test_the_count_is_the_header_field),
		cmocka_unit_test(test_every_offset_is_the_one_sqlite_wrote),
		cmocka_unit_test(test_the_pointers_descend_while_the_keys_ascend),
		cmocka_unit_test(test_an_offset_is_measured_from_the_start_of_the_page),
		cmocka_unit_test(test_an_entry_past_the_end_is_refused),
		cmocka_unit_test(test_a_count_larger_than_the_page_is_refused),
		cmocka_unit_test(test_each_cell_decodes),
		cmocka_unit_test(test_a_cell_is_narrowed_to_its_own_extent),
		cmocka_unit_test(test_a_nine_byte_rowid_decodes),
	};

	return cmocka_run_group_tests(tests, NULL, NULL);
}
