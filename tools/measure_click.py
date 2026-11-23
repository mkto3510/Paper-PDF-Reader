"""Time a real click on an in-text citation or figure mention until the view moves.

For local use only (it opens PDFs that are not in the repository), so it is not
named check_*.py. It drives the real window with the offscreen Qt platform, so
the numbers include the worker queue, the per-page scan requests and the click
timer. The engine-only times come from measure_latency.py.

  python tools/measure_click.py PATH.pdf [--kind reference|figure] [--page N]

Prints one JSON object: milliseconds from the mouse press to the view reaching
the target page, plus the same click repeated once the indexes are warm.
"""

from __future__ import annotations

import argparse
from multiprocessing import freeze_support
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def find_click(path, kind, first_page):
    """Return (page, (x, y), target_page) for a mention whose target the engine can resolve."""
    from paper_reader.pdf import PdfEngine

    engine = PdfEngine()
    engine.open(str(path))
    try:
        step = engine.figure_step if kind == "figure" else engine.reference_step
        while not step():
            pass
        lookup = engine.figure_targets if kind == "figure" else engine.reference_targets
        pages = engine.doc.page_count
        for page in list(range(first_page, pages)) + list(range(0, first_page)):
            lines = engine.text(page, quads=False)
            for line in lines:
                if not line.horizontal:
                    continue
                left, top, right, bottom = line.box
                x = left
                while x < right:
                    y = (top + bottom) / 2
                    citation = engine.citation_at(page, x, y)
                    # A citation with several keys opens a menu and waits for a choice.
                    if citation is not None and citation.kind == kind and citation.box and len(citation.keys) == 1:
                        cx = (citation.box[0] + citation.box[2]) / 2
                        cy = (citation.box[1] + citation.box[3]) / 2
                        targets = lookup((page, cx, cy), citation.keys, True) if kind == "figure" \
                            else lookup((page, cx, cy), citation.keys, ordered=True)
                        target = next((item for item in targets if item), None)
                        if target is not None and target.page != page:
                            return page, (cx, cy), target.page
                    x += 12
        return None
    finally:
        engine.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdf")
    parser.add_argument("--kind", choices=("reference", "figure"), default="reference")
    parser.add_argument("--page", type=int, default=0, help="first page to search for a mention")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--trace", action="store_true", help="print where input_revision changes during a click")
    args = parser.parse_args()
    path = Path(args.pdf)
    found = find_click(path, args.kind, args.page)
    result = {"file": path.name, "kind": args.kind}
    if found is None:
        result["error"] = "no resolvable mention found"
        print(json.dumps(result, ensure_ascii=False))
        return
    page, point, target = found
    result.update(source_page=page, target_page=target)

    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from paper_reader.application import Application

    app = QApplication([])
    with TemporaryDirectory() as folder:
        control = Application(app, Path(folder))
        control.preferences.data.update(language="en", autoSave=False)
        window = control.new_window()
        try:
            window.open_paths([str(path)])
            deadline = time.perf_counter() + args.timeout
            while not (window.pane and window.pane.view.pages):
                QTest.qWait(10)
                if time.perf_counter() > deadline:
                    result["error"] = "open timeout"
                    print(json.dumps(result, ensure_ascii=False))
                    return
            view = window.pane.view
            window.resize(950, 710)
            view.set_zoom("fit")

            if args.trace:
                import traceback
                cls = type(view)
                state = {"on": False}

                def get(self):
                    return self.__dict__.get("_ir", 0)

                def put(self, value):
                    if state["on"] and value != self.__dict__.get("_ir", 0):
                        print("input_revision changed:", "".join(traceback.format_stack(limit=7)[:-1]),
                              file=sys.stderr, flush=True)
                    self.__dict__["_ir"] = value

                cls.input_revision = property(get, put)
                view.__dict__["_ir"] = 0

            def click_once():
                view.go_to(page)
                QTest.qWait(300)
                position = view._project(page, point).toPoint()
                before = view.location(), view.viewport_revision, view.input_revision
                if args.trace:
                    state["on"] = True
                started = time.perf_counter()
                ready_at = None
                QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=position)
                while abs(view.current_page() - target) > 1:
                    QTest.qWait(5)
                    if ready_at is None and args.kind in window.pane.document.references_ready:
                        ready_at = round((time.perf_counter() - started) * 1000)
                    if time.perf_counter() - started > args.timeout:
                        result["stalled"] = {"page": view.current_page(),
                                             "ready": sorted(window.pane.document.references_ready),
                                             "scanning": sorted(window.pane.document.reference_scans),
                                             "lookup_alive": window.pane.lookup is not None,
                                             "visible": window.pane.isVisible(),
                                             "location_moved": view.location() != before[0],
                                             "viewport_revision_changed": view.viewport_revision != before[1],
                                             "input_revision_changed": view.input_revision != before[2]}
                        pane = window.pane
                        if pane.lookup is not None:
                            captured = dict(zip(pane.lookup.__code__.co_freevars,
                                                (cell.cell_contents for cell in pane.lookup.__closure__)))
                            document = pane.document
                            result["stalled"]["lookup_current"] = pane.lookup()
                            result["stalled"]["checks"] = {
                                "generation": captured.get("generation") == document.generation,
                                "input": captured.get("revision") == view.input_revision,
                                "rules": captured.get("rules_generation") == document.rules_generation,
                                "selected": captured.get("selected") == view.selected_note,
                                "viewport": captured.get("viewport_revision") == view.viewport_revision,
                                "location": captured.get("location") == view.location(),
                                "loading": document.loading, "disposed": document.disposed}
                            result["stalled"]["location"] = [captured.get("location"), view.location()]
                        return None, ready_at
                return round((time.perf_counter() - started) * 1000), ready_at

            result["first_click_ms"], result["first_index_ready_ms"] = click_once()
            result["second_click_ms"], _ = click_once()
        finally:
            control.shutdown()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    freeze_support()
    main()
