"""DevCake host CLI (userspace console script).

Import package name is ``devcake_cli`` so a checkout ``PYTHONPATH=…:app``
cannot shadow ``app/devcake``. Distribution / project name is ``devcake-cli``
(ADR-0038 Decision 3).
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
