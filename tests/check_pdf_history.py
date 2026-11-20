"""Exercise journal transactions and atomic PDF saves across history states."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pymupdf as mu

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_reader.pdf import PdfEngine


def make_pdf(path, *, object_streams=False, encryption=False, certification=None):
    with mu.open() as pdf:
        for _ in range(2):
            page = pdf.new_page()
            page.insert_text((40, 65), "First word in the report")
        if certification is not None:
            # Minimal certification dictionary to exercise policy, without a signing key.
            xref = pdf.get_new_xref()
            pdf.update_object(xref, "<< /Type /Sig /Reference [<< /TransformMethod /DocMDP "
                              f"/TransformParams << /P {certification} >> >>] >>")
            pdf.xref_set_key(pdf.pdf_catalog(), "Perms", f"<< /DocMDP {xref} 0 R >>")
        options = dict(encryption=mu.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="reader") if encryption else {}
        pdf.save(path, use_objstms=int(object_streams), **options)


def contents(path, password=""):
    with mu.open(path) as pdf:
        if pdf.needs_pass:
            assert pdf.authenticate(password)
        return [[a.info["content"] for a in page.annots()] for page in pdf]


def add(engine, comment):
    quads = {index: [engine.text(index)[0].glyphs[0].quad] for index in (0, 1)}
    return engine.edit("add_highlight", (quads, "#2ea8e5", comment))


def check_history(root, *, object_streams):
    path = root / "history.pdf"
    backup = root / "backups"
    make_pdf(path, object_streams=object_streams)
    original = path.read_bytes()
    engine = PdfEngine(root)
    try:
        engine.open(str(path))
        added = add(engine, "Report")
        keys = added["keys"]
        assert engine.save(str(backup), 1)["state"] == added["state"]
        assert path.read_bytes().startswith(original)
        assert next(backup.iterdir()).read_bytes() == original

        edited = engine.edit("modify_highlight", (keys, "#ff6666", "Revised"))
        try:
            engine.edit("modify_highlight", ((keys[0], (1, 99999)), None, None, True))
        except (RuntimeError, ValueError):
            pass
        else:
            raise AssertionError("Invalid annotation accepted")
        assert len(engine.annotation_list()) == 2
        assert engine.doc.journal_position() == (2, 2)

        deleted = engine.edit("modify_highlight", (keys, None, None, True))
        saved_bytes = path.read_bytes()
        with patch("paper_reader.pdf.os.replace", side_effect=PermissionError("File in use")):
            try:
                engine.save(str(backup), 1)
            except PermissionError:
                pass
            else:
                raise AssertionError("Replacement failure was ignored")
        assert path.read_bytes() == saved_bytes
        assert engine.saved_revision == added["state"]
        assert engine.save(str(backup), 1)["state"] == deleted["state"]
        assert contents(path) == [[], []]

        restored = engine.move_history(False)
        assert restored["state"] == edited["state"] and restored["redo"]
        assert [(note["page"], note["xref"]) for note in engine.annotation_list()] == list(keys)
        copy = root / "copy.pdf"
        engine.save(str(backup), 1, str(copy))
        assert contents(copy) == [["Revised"], ["Revised"]]
        assert contents(path) == [[], []]
        assert copy.read_bytes().startswith(original)
        engine.save(str(backup), 1)
        assert engine.move_history(True)["state"] == deleted["state"]
        engine.save(str(backup), 1)
        assert contents(path) == [[], []] and path.read_bytes().startswith(original)
        engine.move_history(False)
        branch = engine.edit("modify_highlight", (keys, None, "Branch"))
        assert deleted["state"] in branch["discarded"] and not branch["redo"]

        path.write_bytes(original + b"\n% External edit\n")
        try:
            engine.save(str(backup), 1)
        except ValueError:
            pass
        else:
            raise AssertionError("External change was overwritten")
        assert path.read_bytes().endswith(b"% External edit\n")
        engine.save(str(backup), 1, str(copy))
        assert contents(copy) == [["Branch"], ["Branch"]]
    finally:
        engine.close()


def check_save_options(root):
    backup = str(root / "backups")
    engine = PdfEngine(root)
    try:
        path = root / "encrypted.pdf"
        make_pdf(path, encryption=True)
        original = path.read_bytes()
        assert engine.open(str(path), "reader")["pages"]
        add(engine, "Encrypted")
        engine.save(backup, 0)
        assert path.read_bytes().startswith(original)
        assert contents(path, "reader") == [["Encrypted"], ["Encrypted"]]

        for permission in (None, 2, 3):
            path = root / f"certification-{permission}.pdf"
            make_pdf(path, certification=permission)
            original = path.read_bytes()
            engine.open(str(path))
            add(engine, "Changes")
            if permission == 2:
                copy = root / "annots.pdf"
                assert engine.save(backup, 0) == {"signature_blocked": True}
                assert engine.save(backup, 0, str(copy)) == {"confirm_signature_loss": True}
                assert not copy.exists() and path.read_bytes() == original
                engine.save(backup, 0, str(copy), True)
                assert copy.read_bytes().startswith(original)
            else:
                engine.save(backup, 0)
                assert path.read_bytes().startswith(original)

            # Remove startxref so MuPDF has to repair the PDF on opening.
            engine.close()
            broken = original[:original.rindex(b"startxref")]
            path.write_bytes(broken)
            engine.open(str(path))
            assert not engine.doc.can_save_incrementally()
            add(engine, "After repair")
            if permission is None:
                engine.save(backup, 0)
                assert contents(path) == [["After repair"], ["After repair"]]
                assert engine.move_history(False)["redo"]
                engine.save(backup, 0)
                assert contents(path) == [[], []]
            else:
                assert engine.save(backup, 0) == {"signature_blocked": True}
                assert path.read_bytes() == broken
    finally:
        engine.close()


if __name__ == "__main__":
    for object_streams in (False, True):
        with TemporaryDirectory() as folder:
            check_history(Path(folder), object_streams=object_streams)
    with TemporaryDirectory() as folder:
        check_save_options(Path(folder))
    print("PASS annotation journal, repeated saves, encryption, certification policy, and atomic replacement")
