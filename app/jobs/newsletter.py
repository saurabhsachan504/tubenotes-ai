"""Send the opt-in TubeNotes newsletter from host cron.

Run every minute; the job sends only Wednesday and Sunday at 09:00 IST:
``docker compose -p tubenotes-ai -f docker-compose.shared-db.prod.yml exec -T api python -m app.jobs.newsletter --due``
"""
from __future__ import annotations

import argparse

from app.database import SessionLocal
from app.services import newsletter


def main() -> int:
    parser = argparse.ArgumentParser(description="Run TubeNotes opt-in newsletter")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--due", action="store_true", help="Send only when the current IST minute is scheduled")
    group.add_argument("--force", action="store_true", help="Send one scheduled issue now to every opted-in recipient")
    group.add_argument("--test-email", metavar="EMAIL", help="Send a visual preview only to one address")
    args = parser.parse_args()
    db = SessionLocal()
    try:
        result = (
            newsletter.send_test_email(args.test_email)
            if args.test_email
            else newsletter.run_due(db, force=args.force)
        )
        print("newsletter", " ".join(f"{key}={value}" for key, value in result.items()))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
