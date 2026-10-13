"""Run only after inspecting generated inventory.py; synthetic arguments only."""
import copy
import importlib.util
import sys
from pathlib import Path


def check(project):
    spec = importlib.util.spec_from_file_location('synthetic_inventory', Path(project)/'inventory.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cases = [([], 0, []), ([{'item':'a','quantity':5}], 5, ['a']),
             ([{'item':'a','quantity':3},{'item':'a','quantity':4}], 5, []),
             ([{'item':'本','quantity':1},{'item':'箱','quantity':0}], 1, ['本','箱'])]
    for records, threshold, expected in cases:
        before = copy.deepcopy(records)
        assert module.low_stock(records, threshold) == expected
        assert records == before
    for records in ([{'item':'a','quantity':-1}], [{'item':'a','quantity':5},{'item':'a','quantity':-2}]):
        try: module.low_stock(records, 5)
        except ValueError: pass
        else: raise AssertionError('negative quantity was not rejected')
    return 6


if __name__ == '__main__':
    project = Path(sys.argv[1])
    count = check(project)
    (project/'EVALUATION_CHECKS.md').write_text(
        '# 独立確認（評価側）\n\ninventory.py: 6ケース合格。空入力、境界、重複合算、日本語名、入力不変、負数拒否。\n'
        'stock_cli.py と README.md はこの確認時点で未作成。CLIと説明の確認は未実施であり、全体合格ではありません。\n', encoding='utf-8')
    print('inventory_checks_passed', count)
