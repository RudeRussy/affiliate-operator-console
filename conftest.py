"""
pytest configuration for the affiliate_stack suite.

The autouse fixture below isolates the outreach DB before every test. It shares one
implementation with script mode (tests.harness.isolate_db), so `pytest` and
`./run.sh test` cannot drift apart.

Why it is mandatory: the test modules share module-level DB state, so without this a
test inherits rows created by whatever ran before it. Script mode happened to run in
an order that worked; pytest runs files alphabetically and failed 5 tests on leftover
data. Order-dependent tests are worse than no tests.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _isolate_db():
    from tests.harness import isolate_db
    isolate_db()
    yield
