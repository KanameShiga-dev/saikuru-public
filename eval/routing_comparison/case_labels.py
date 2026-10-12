"""Synthetic string-processing case for a later isolated routing comparison."""
import ast
from pathlib import Path

FILE = 'labels.py'
INITIAL = ('def normalize_labels(values):\n'
           '    return [value.strip().lower() for value in values if value]\n')
GOAL = ('架空の開発評価です。labels.py だけを変更してください。normalize_labels(values) は'
        '各文字列の前後空白を除き、小文字にして、空白だけになった値を除きます。'
        '順序と重複を保ち、入力リストを変更しません。標準Pythonのみを使います。'
        '1人の制作担当で修正し、通常レビューとセキュリティレビューへ進めてください。'
        '外部通信、別ファイルの編集、削除、インストール、生成コードやテストの実行は禁止です。'
        'プロジェクト内の読み取りだけを許可します。制作後、評価側が独立採点を実施し、'
        'EVALUATION_CHECKS.md に結果を記録します。レビュー担当はこの証拠を読み、'
        '採点の未実施を推測せずに判断してください。予定済みの後続レビュー工程そのものは'
        '未確認の必須検証へ列挙しません。追加質問が必要なら停止してください。')
ACCEPTANCE = ('空配列は空配列、[" A ", " ", "b"] は ["a", "b"]、'
              '["A", "a"] は ["a", "a"]。順序・重複を保ち、入力リストを変更しない。')
CASES = [([], []), ([' A ', ' ', 'b'], ['a', 'b']),
         (['A', 'a'], ['a', 'a']), (['  東京  '], ['東京']),
         (['', '\t'], [])]


def quality(project):
    """Independent restricted checker for the fixed synthetic fixture only."""
    try:
        source = (Path(project) / FILE).read_text(encoding='utf-8')
        tree = ast.parse(source)
        forbidden = (ast.Import, ast.ImportFrom, ast.ClassDef, ast.With, ast.AsyncWith,
                     ast.While, ast.Global, ast.Nonlocal, ast.Lambda, ast.Try, ast.Delete)
        if any(isinstance(node, forbidden)
               or isinstance(node, ast.Attribute) and node.attr not in ('strip', 'lower', 'append')
               or isinstance(node, ast.Name) and node.id.startswith('__')
               for node in ast.walk(tree)):
            return False
        namespace = {'__builtins__': {'len': len, 'range': range, 'enumerate': enumerate}}
        exec(compile(tree, FILE, 'exec'), namespace)
        fn = namespace['normalize_labels']
        for values, expected in CASES:
            original = values[:]
            if fn(values) != expected or values != original:
                return False
        return True
    except Exception:
        return False
