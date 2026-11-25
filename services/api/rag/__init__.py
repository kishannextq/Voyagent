"""Retrieval-augmented generation over each tenant's own documents (travel policies, guides).

ingest:     upload -> validate/scan -> extract -> chunk (parent/child) -> embed -> store
retrieval:  vector + full-text candidates -> reciprocal-rank fusion -> rerank -> parents -> citations
Every read and write is scoped to one tenant. Retrieved text is data, never instructions.
"""
