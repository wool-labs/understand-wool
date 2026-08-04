"""Root conftest — assert the interpreter floor before anything is collected.

`pyproject.toml` declares Python >= 3.11, but a `requires-python` key only has
a home under `[project]`, and this repository is deliberately not a Python
package. Without a check the floor would be a comment, and a developer on an
older interpreter would meet it as a collection error somewhere inside a suite
rather than as a statement about their environment.

This runs at import time, before collection, so the message arrives first.
"""

from __future__ import annotations

import sys

MINIMUM_PYTHON = (3, 11)

if sys.version_info < MINIMUM_PYTHON:  # pragma: no cover — the floor itself
    raise RuntimeError(
        f"This test suite requires Python "
        f"{'.'.join(map(str, MINIMUM_PYTHON))} or newer; "
        f"found {sys.version.split()[0]} at {sys.executable}. "
        f"Install the dependencies with `python -m pip install --group dev` "
        f"on a supported interpreter."
    )
