"""Licensing / data-source compliance gates.

Each module here owns ONE data source's redistribution rule, so the answer to
"may this caller see X?" lives in a single importable place rather than being
re-derived at every surface. docs/architecture.md (Data sources) summarises
each provider's terms.
"""
