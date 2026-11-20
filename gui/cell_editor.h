// cell_editor.h -- the widget a value cell is edited with, by what the
// field holds (26.1114).
//
// Every constraint here is the view's (editor/view.py): the edit kind, an
// enum's members and whether it admits others, a byte run's length. The
// widget only refuses to TYPE what the core would refuse to store, so a
// person learns at the key rather than from a refusal after Enter -- and
// the core still decides, since `--put` is what a commit sends.
//
// - `choice`: an editable combo of the enum's members with a completer that
//   matches anywhere in a name, so typing `cast` finds `broadcast (5)`. A
//   closed enum takes only text some member contains; an open one takes a
//   number as well.
// - `number`: decimal, or hex after `0x`, with a sign.
// - `hex`: hex digits, no more than the run holds.
//
// Text the validator does not ACCEPT is never committed -- not on Return
// and not on focus leaving -- because Qt's own delegate commits whatever an
// editor holds when it closes, and a half-typed `serv` would otherwise go to
// the core as a number.
//
// Stock widgets only, so qtty draws and drives them on a terminal.

#ifndef SITU_GUI_CELL_EDITOR_H
#define SITU_GUI_CELL_EDITOR_H

#include <QStyledItemDelegate>

// Where a value cell keeps its row of the view: a QVariantMap of `edit`,
// `choices`, `admits_other` and `size`.
constexpr int view_row_role = Qt::UserRole + 1;

class cell_editor : public QStyledItemDelegate {
	Q_OBJECT

public:
	using QStyledItemDelegate::QStyledItemDelegate;

	QWidget *createEditor(QWidget *parent, const QStyleOptionViewItem &option,
	                      const QModelIndex &index) const override;
	void setEditorData(QWidget *editor,
	                   const QModelIndex &index) const override;
	void setModelData(QWidget *editor, QAbstractItemModel *model,
	                  const QModelIndex &index) const override;
};

#endif
