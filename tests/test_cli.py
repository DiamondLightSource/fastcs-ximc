import json
import subprocess
import sys
from pathlib import Path

from fastcs_ximc import __version__

SCHEMA_PATH = Path(__file__).parent.parent / "schema.json"


def test_cli_version():
    cmd = [sys.executable, "-m", "fastcs_ximc", "--version"]
    output = subprocess.check_output(cmd).decode()
    assert __version__ in output


def test_schema_is_up_to_date():
    """The checked-in schema must match the current options models."""
    cmd = [sys.executable, "-m", "fastcs_ximc", "schema"]
    generated = json.loads(subprocess.check_output(cmd).decode())
    committed = json.loads(SCHEMA_PATH.read_text())
    assert generated == committed, (
        "schema.json is stale - regenerate with "
        "`python -m fastcs_ximc schema > schema.json`"
    )
