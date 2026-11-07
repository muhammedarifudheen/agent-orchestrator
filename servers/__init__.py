"""Makes ``import servers.<name>_server`` work.

Servers import their siblings as top-level modules (``from _base import ...``) because they are
launched as scripts (``python servers/x_server.py``, which is how the registry and ``mcp dev``
start them), and a script's directory is put on ``sys.path`` automatically. Importing a server as
part of this package must see the same sibling modules, so put this directory on ``sys.path``.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
