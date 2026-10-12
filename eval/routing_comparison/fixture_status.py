"""Print status only for the synthetic human-acceptance fixture."""
import json
import sqlite3
import sys
from pathlib import Path


def main():
    database = Path(sys.argv[1]) / 'runtime' / 'team.sqlite3'
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
        objects = [(kind, json.loads(body)) for kind, body in db.execute(
            "SELECT kind,body FROM objects WHERE kind IN ('job','approval')")]
        events = [json.loads(body)['type'] for (body,) in db.execute('SELECT body FROM events')]
    print(json.dumps({'job_statuses': [obj.get('status') for kind, obj in objects if kind == 'job'],
                      'approval_statuses': [obj.get('status') for kind, obj in objects if kind == 'approval'],
                      'resolved_check_counts': [len((obj.get('payload') or {}).get('resolved_checks') or [])
                                                for kind, obj in objects if kind == 'approval'],
                      'event_types': events}, ensure_ascii=False))


if __name__ == '__main__':
    main()
