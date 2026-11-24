"""Count the pages where reading order jumps back up inside one column, in a dump from dump_analysis.py.

For local use only.

  python tools/count_order_jumps.py dump.jsonl [other.jsonl]

In the lines of a page (body lines only, in the order a selection reads them), a jump is a line that lies in the same
column as the line before it but five or more line heights above it. A page with such a jump reads its text out of
order. With two dumps, the pages whose count changed are listed.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys


def jumps(lines):
    body = [line for line in lines if not line[0] and line[1].strip()]
    count = 0
    for a, b in zip(body, body[1:]):
        height = max(a[2][3] - a[2][1], b[2][3] - b[2][1], 1)
        overlap = min(a[2][2], b[2][2]) - max(a[2][0], b[2][0])
        if overlap > min(a[2][2] - a[2][0], b[2][2] - b[2][0]) * .5 and b[2][1] < a[2][1] - height * 5:
            count += 1
    return count


def load(name):
    result = {}
    for line in Path(name).read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if "skipped" in record or "error" in record:
            continue
        result[record["key"]] = {int(page): jumps(lines) for page, lines in record.get("text", {}).items()}
    return result


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    first = load(sys.argv[1])
    pages = sum(len(value) for value in first.values())
    bad = [(key, page) for key, value in first.items() for page, count in value.items() if count]
    print(f"{sys.argv[1]}: {pages} pages in {len(first)} documents; pages with a jump: {len(bad)}"
          f" in {len({key for key, _ in bad})} documents")
    if len(sys.argv) > 2:
        second = load(sys.argv[2])
        worse, better = [], []
        for key in sorted(first.keys() & second.keys()):
            for page in sorted(first[key].keys() & second[key].keys()):
                a, b = first[key][page], second[key][page]
                if b > a:
                    worse.append((key, page, a, b))
                elif b < a:
                    better.append((key, page, a, b))
        bad_after = sum(1 for key, value in second.items() for count in value.values() if count)
        print(f"{sys.argv[2]}: pages with a jump: {bad_after}; fewer jumps on {len(better)} pages, more on {len(worse)}")
        for label, items in (("MORE jumps", worse), ("fewer jumps", better)):
            for item in items[:40]:
                print(" ", label, item)


if __name__ == "__main__":
    main()
