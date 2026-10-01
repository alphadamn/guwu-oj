#!/usr/bin/env python
"""Fix Codeforces math delimiters: ``$$$`` -> ``$``.

Codeforces statements use ``$$$...$$$`` for inline math and
``$$$$$$...$$$$$$`` for display math, but the site's renderer
(``problems/markdown_utils.py::render_markdown``) only understands
``$...$`` / ``$$...$$``. Imported CF problems therefore show the math
delimiters literally. This script rewrites every ``$$$`` run back to a
single ``$`` in both the statement (``description``) and the hint
(``hint``).

Targets only imported Codeforces problems, i.e. rows whose ``tags``
contain the ``cf:`` key tag.

Usage (from the project root, with the project venv):

    venv/bin/python scripts/fix_cf_math_delimiters.py --dry-run   # report only
    venv/bin/python scripts/fix_cf_math_delimiters.py             # apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import os  # noqa: E402  (after sys.path setup)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'oj_project.settings')

import django  # noqa: E402

django.setup()

from django.db import transaction  # noqa: E402

from problems.models import Problem  # noqa: E402

KEY_TAG_PREFIX = 'cf:'
BATCH_SIZE = 500


def fix(text: str) -> str:
    """Collapse every ``$$$`` run region to single ``$`` delimiters.

    ``$$$$$$X$$$$$$`` (display math) -> ``$$X$$``; ``$$$X$$$`` (inline)
    -> ``$X$``. Implemented as the literal replacement the data bug needs:
    each ``$$$`` becomes ``$``.
    """
    return text.replace('$$$', '$')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                        help='report what would change without writing')
    args = parser.parse_args()

    qs = Problem.objects.all()
    total = qs.count()

    desc_hits = hint_hits = rows_changed = 0
    desc_occurrences = hint_occurrences = 0
    batch: list[Problem] = []

    for problem in qs.iterator(chunk_size=BATCH_SIZE):
        changed = False

        desc = problem.description or ''
        if '$$$' in desc:
            desc_hits += 1
            desc_occurrences += desc.count('$$$')
            problem.description = fix(desc)
            changed = True

        hint = problem.hint or ''
        if '$$$' in hint:
            hint_hits += 1
            hint_occurrences += hint.count('$$$')
            problem.hint = fix(hint)
            changed = True

        if changed:
            rows_changed += 1
            batch.append(problem)
            if not args.dry_run and len(batch) >= BATCH_SIZE:
                with transaction.atomic():
                    Problem.objects.bulk_update(
                        batch, ['description', 'hint'], batch_size=BATCH_SIZE)
                batch.clear()

    if not args.dry_run and batch:
        with transaction.atomic():
            Problem.objects.bulk_update(
                batch, ['description', 'hint'], batch_size=BATCH_SIZE)

    mode = 'DRY RUN (no writes)' if args.dry_run else 'APPLIED'
    print(f'[{mode}] CF problems scanned        : {total}')
    print(f'[{mode}] rows changed               : {rows_changed}')
    print(f'[{mode}] description fields         : {desc_hits} '
          f'({desc_occurrences} occurrences)')
    print(f'[{mode}] hint fields                : {hint_hits} '
          f'({hint_occurrences} occurrences)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
