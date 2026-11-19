"""Work files never go into the code folder.

The server may run from a read-only or shared network folder. Generated C#
scripts, their result files, batch requests and logs therefore live in the
local TEMP work folder (connection.logs.WORK_ROOT = %TEMP%/eplan_mcp), never
next to the code. EPLAN_MCP_LOG_DIR still overrides the log folder.
"""

import os
import tempfile

from api.actions import ops, scripted
from connection import logs

_CODE_DIR = os.path.normcase(os.path.dirname(os.path.abspath(logs.__file__ + "/..")))


def _outside_code(path):
    p = os.path.normcase(os.path.abspath(path))
    return not (p == _CODE_DIR or p.startswith(_CODE_DIR + os.sep))


def test_work_root_is_the_local_temp():
    assert logs.WORK_ROOT == os.path.join(tempfile.gettempdir(), "eplan_mcp")
    assert _outside_code(logs.WORK_ROOT)


def test_generated_scripts_and_results_outside_the_code_folder():
    for path in (scripted.SCRIPT_DIR, scripted.RESULTS_DIR, ops.OPS_DIR):
        assert _outside_code(path), path
        assert path.startswith(logs.WORK_ROOT), path


def test_audit_scripts_outside_the_code_folder():
    assert _outside_code(scripted.AUDIT_SCRIPT_DIR)


def test_default_log_dir_outside_the_code_folder(monkeypatch):
    monkeypatch.delenv("EPLAN_MCP_LOG_DIR", raising=False)
    assert logs._log_dir() == os.path.join(logs.WORK_ROOT, "logs")
    assert _outside_code(logs._log_dir())


def test_quiet_mode_wrapper_source_uses_the_work_root():
    """The generated action wrapper's folders come from WORK_ROOT."""
    import inspect
    from connection import action_scaffold
    src = inspect.getsource(action_scaffold)
    assert "base_dir = WORK_ROOT" in src
    assert "os.path.dirname(os.path.dirname(os.path.abspath(__file__)))" not in src
