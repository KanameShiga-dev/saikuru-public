"""Human acceptance screen for a finished due-date evaluation. Never discovers or runs a provider.

Serves the evaluation's own runtime DB with the engine paused. Only acceptance decisions,
evidence and effort records can be posted. The agent never presses the acceptance buttons.
"""
import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from eval.development_readiness.reacceptance_fixture import close, make_fixture
from eval.development_readiness.run_due_date import validate_base


def ensure_single_job(database):
    # make_fixture seeds a demo job into an empty DB; check read-only first so evidence is never mixed.
    with closing(sqlite3.connect(f'file:{Path(database).as_posix()}?mode=ro', uri=True)) as db:
        jobs = db.execute("SELECT COUNT(*) FROM objects WHERE kind='job'").fetchone()[0]
    if jobs != 1:
        raise ValueError('The evaluation DB must hold exactly one job')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8819)
    args = parser.parse_args()
    base = validate_base(args.base)
    manifest = json.loads((base/'manifest.json').read_text(encoding='utf-8'))
    database = base/'runtime'/'team.sqlite3'
    if manifest.get('purpose') != 'due-date-live-evaluation' or not database.exists():
        raise ValueError('Not a finished due-date evaluation base')
    ensure_single_job(database)
    server = make_fixture(base, args.port, project_dir='development')
    print(server.app.origin, flush=True)
    try:
        server.serve_forever()
    finally:
        close(server)


if __name__ == '__main__':
    main()
