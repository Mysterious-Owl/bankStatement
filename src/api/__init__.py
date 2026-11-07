"""
HTTP layer for the statement analytics app.

`server.py` was a 1600-line module holding page routes, ingestion, enrichment,
tagging and reconciliation side by side. It is split here by concern: one
router per area, with the pieces they share (JSON persistence, the enriched
frame, criteria matching) in modules of their own so a route file contains
routes and nothing else.
"""
