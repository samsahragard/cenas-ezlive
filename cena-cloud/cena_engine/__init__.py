"""C.E.N.A. Level 3 reasoning engine - vendored, self-contained package.

This is a verbatim copy of the eight ``cena_*.py`` engine modules from the CK
runtime (``app/services/``), with ONLY their inter-module import paths rewritten
from ``app.services.cena_X`` to ``cena_engine.cena_X`` so the engine runs inside
the cena-cloud service with no ``app`` package present. The SQL/validation/
allowlist logic is unchanged.

Entry point: ``cena_engine.cena_sql_orchestrator.answer_question``.
"""
