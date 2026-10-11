"""founder-kindle-loop social pipeline (trigger A this pass).

Two modules, deliberately separate:
  * daily_card.py    — assembles ONE candidate/day (caption + chart image).
  * postiz_client.py — stages a candidate into Postiz as a DRAFT. NEVER
                        schedules/publishes — see that module's docstring.

No Flask/FastAPI routes are wired here yet (CLI-only this pass); nothing in
this package is imported by the running app, so it carries zero runtime risk
until the operator decides to wire a cron (deferred).
"""
