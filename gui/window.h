// window.h -- the Qt Widgets frontend of decision 0034.
//
// A table of the document's fields, with the value column editable where
// `situ-edit` says the field is writable, and a status line for what a write
// cost or why it was refused. Stock widgets only, and no painting of its
// own, so that qtty can put the same window on a terminal.

#ifndef SITU_GUI_WINDOW_H
#define SITU_GUI_WINDOW_H

#include "session.h"

#include <QMainWindow>

class QLabel;
class QTableWidget;
class QTableWidgetItem;

class edit_window : public QMainWindow {
	Q_OBJECT

public:
	explicit edit_window(QWidget *parent = nullptr);

	// Opens a message against a schema; false with the reason shown in the
	// window when `situ-edit` refuses it.
	bool open(const open_options &options, const QString &out);

	// What the last refusal said, for a caller that never shows the window.
	QString error() const { return session_.error(); }

	// The status line, which is where an edit's cost or refusal is said.
	QString status_text() const;

	// The table as lines, for `--no-ui`: what the window shows, not what
	// the CLI prints, so a cell the window got wrong is visible to a test.
	QString as_text() const;

	// Save to `--out` without a dialog. False with the reason in `error()`.
	bool write_out();

protected:
	void closeEvent(QCloseEvent *event) override;

private:
	void populate();
	void edited(QTableWidgetItem *item);
	void open_dialog();
	bool save();
	bool save_as();
	void say(const QString &text);

	session           session_;
	QString           out_;
	QLabel           *header_  = nullptr;
	QTableWidget     *table_   = nullptr;
	bool              filling_ = false;
};

#endif
