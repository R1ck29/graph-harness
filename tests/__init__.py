"""Contract tests for the vendor-neutral task-graph core.

Importing this package points harness state at a throwaway directory for the
whole run. The library records observations under the user's home, so without
this a test that runs ``graphctl`` or a hook would append to the developer's
own journal, and a CI run would append to the runner's. Individual tests still
set the variable themselves when they need to read back what they wrote.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

# Set unconditionally rather than deferring to an exported value. No test
# needs the deference — the ones that care set the variable themselves in a
# subprocess environment — while deferring would let a developer or a CI job
# that happens to export it write records into real harness state, which is
# the very thing this exists to prevent.
_isolated_home = tempfile.mkdtemp(prefix="graph-harness-tests.")
os.environ["GRAPH_HARNESS_HOME"] = _isolated_home
atexit.register(shutil.rmtree, _isolated_home, True)
