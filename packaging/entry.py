"""Frozen-build entry point.

PyInstaller runs its entry script as ``__main__``, with no package context — so
pointing it at ``src/dropwatch/__main__.py`` makes every ``from . import ...`` in
that module fail with "attempted relative import with no known parent package".

Importing the package properly and calling into it keeps the relative imports
valid, and means the exe and ``python -m dropwatch`` run identical code.
"""

from __future__ import annotations

import multiprocessing
import sys

from dropwatch.__main__ import main

if __name__ == "__main__":
    # Harmless here, and the standard guard against a frozen exe re-spawning
    # itself if anything ever reaches for multiprocessing.
    multiprocessing.freeze_support()
    sys.exit(main())
