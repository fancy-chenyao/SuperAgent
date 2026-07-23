"""Execution-engine orchestration package.

Intentionally kept import-light (no package-level imports) so that the pure
data-plane modules (:mod:`schema_registry`, :mod:`store`, :mod:`resolver`) can be
imported and unit-tested without pulling in the workflow/manager/LLM stack.
"""
