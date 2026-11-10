"""Test-wide defaults.

The package ships a deliberately generic ``default_author`` because it is
public. The store fixtures below are written around a named one, so pin it
here rather than letting every assertion depend on whatever the shipped
default happens to be — that coupling is what makes a harmless rename break
twenty tests at once.
"""

from __future__ import annotations

import pytest

from notes import store

DEFAULT_AUTHOR = "sam"


@pytest.fixture(autouse=True)
def _default_author():
    before = store.HUMAN
    store.HUMAN = DEFAULT_AUTHOR
    yield
    store.HUMAN = before
