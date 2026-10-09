"""Run due Daily/Weekly browser-push campaigns from host cron.

Example (host crontab, once each minute):
``docker exec tubenotes-ai-app python -m app.jobs.push_campaigns --due``
"""
from __future__ import annotations

import argparse

from app.database import SessionLocal
from app.services import push_campaigns


def main() -> int:
    parser = argparse.ArgumentParser(description="Run TubeNotes browser push campaigns")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--due", action="store_true", help="Run campaigns due in the current IST minute")
    group.add_argument("--kind", choices=("daily", "weekly"), help="Run one enabled campaign now")
    args = parser.parse_args()
    db = SessionLocal()
    try:
        campaigns = push_campaigns.ensure_campaigns(db)
        db.commit()
        if args.due:
            selected = push_campaigns.due_campaigns(db)
            trigger = "scheduled"
        else:
            selected = [campaigns[args.kind]]
            trigger = "manual"

        queued = []
        stamp = push_campaigns.now()
        for campaign in selected:
            if not campaign.enabled:
                continue
            key = (
                push_campaigns.scheduled_key(campaign, at=stamp)
                if trigger == "scheduled"
                else None
            )
            run = push_campaigns.queue_run(db, campaign, trigger=trigger, trigger_key=key)
            if run is not None:
                queued.append(run.id)
        for run_id in queued:
            push_campaigns.execute_run(run_id)
        print(f"push campaigns completed: {len(queued)}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
