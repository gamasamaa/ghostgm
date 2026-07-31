"""Run the JS geometry checks through pytest, so `pytest` stays one command.

Skips rather than fails when node isn't installed — node is not a dependency
of the project, only a convenience for exercising the browser-side maths.
"""

import os
import shutil
import subprocess

import pytest

SUITE = os.path.join(os.path.dirname(__file__), "test_geometry.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_geometry_suite():
    result = subprocess.run(["node", SUITE], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout
