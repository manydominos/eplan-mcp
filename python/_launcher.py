"""Console entry point ``eplan-mcp`` (added by manydominos, fork packaging).

Runs ``mcp_server/server.py`` exactly as ``python mcp_server/server.py`` does, from the
installed copy of this folder, so every path the server derives from ``__file__`` stays
the same. Transport and mode come from the environment as before (``MCP_TRANSPORT``,
default stdio; ``EPLAN_MCP_MODE``).
"""
import os
import runpy
import sys


def main() -> None:
    server_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server")
    sys.path.insert(0, server_dir)
    runpy.run_path(os.path.join(server_dir, "server.py"), run_name="__main__")


if __name__ == "__main__":
    main()
