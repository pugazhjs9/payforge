"""The background worker: outbox relay, webhook delivery and scheduled jobs.

Run it with `python -m payforge.worker`. Several copies can run at once: every job is
written to be safe when another worker does the same thing at the same time.
"""
