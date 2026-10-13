"""Offline preparation and human-adjudicated scoring. No AI or network invocation."""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def score(judgments):
    truth = {d['id'] for d in json.loads((ROOT / 'cases.json').read_text(encoding='utf-8'))['defects']}
    if set(judgments) != {'case_id', 'completed', 'adjudicator', 'findings'}:
        raise ValueError('Unexpected fields')
    if (judgments['case_id'] != 'synthetic-stock-v1' or judgments['completed'] is not True
            or judgments['adjudicator'] not in ('human', 'assistant') or not isinstance(judgments['findings'], list)):
        raise ValueError('A completed review and an explicit adjudicator are required')
    found, false_positives, identifiers = set(), 0, set()
    duplicates = 0
    for finding in judgments['findings']:
        if set(finding) != {'finding_id', 'defect_id'}:
            raise ValueError('Use finding IDs and a known defect ID or null; no review text')
        ident = finding['finding_id']
        if type(ident) is not int or ident < 1 or ident in identifiers:
            raise ValueError('Finding IDs must be unique positive integers')
        identifiers.add(ident)
        defect = finding['defect_id']
        if defect is None:
            false_positives += 1
        elif isinstance(defect, str) and defect in truth:
            duplicates += int(defect in found)
            found.add(defect)
        else:
            raise ValueError('Unknown defect ID')
    return {'case_id': judgments['case_id'], 'adjudication': judgments['adjudicator'],
            'true_positives': len(found), 'false_negatives': sorted(truth - found),
            'false_positives': false_positives, 'duplicate_findings': duplicates,
            'recall': len(found) / len(truth),
            'precision': len(found) / (len(found) + false_positives) if found or false_positives else None}


def prepare(destination):
    destination = Path(destination).resolve()
    # A fresh directory prevents overwriting any user project or previous evidence.
    destination.mkdir(parents=True, exist_ok=False)
    (destination / 'inventory.py').write_bytes((ROOT / 'inventory_seed.py').read_bytes())
    (destination / 'REQUEST.md').write_text(
        '# Synthetic stock review\n\nReview inventory.py without editing it. '
        'low_stock(records, threshold) must sum quantities for duplicate item names, '
        'return sorted names whose totals are less than or equal to threshold, '
        'reject negative quantities with ValueError, preserve input records, and return [] for empty input. '
        'Report concrete defects with locations, examples, and expected behavior. '
        'Do not report style preferences as defects. Do not access other directories or the network.\n', encoding='utf-8')
    return {'prepared': True, 'case_id': 'synthetic-stock-v1', 'ai_executed': False}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('prepare').add_argument('destination', type=Path)
    sub.add_parser('score').add_argument('judgments', type=Path)
    args = parser.parse_args()
    result = prepare(args.destination) if args.command == 'prepare' else score(json.loads(args.judgments.read_text(encoding='utf-8')))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
