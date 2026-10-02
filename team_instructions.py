"""Remove repeated retry text without inferring whether different orders agree."""
import re


MARKER = '\n利用者の補足: '


def _units(text):
    # Keep paths, code, enumerations and numbers intact. Japanese sentence
    # boundaries and newlines are the only supported units of comparison.
    return re.findall(r'[^。\n]+。?|\n+', text.replace('\r\n', '\n'))


def compact_instruction(instruction, note=''):
    blocks = str(instruction).split(MARKER)
    if str(note).strip():
        blocks.append(str(note).strip())
    seen = set()
    kept = []
    for block in blocks:
        pieces = []
        for unit in _units(block):
            if not unit.strip():
                pieces.append(unit)
                continue
            key = ' '.join(unit.split())
            if key in seen:
                continue
            seen.add(key)
            pieces.append(unit)
        value = ''.join(pieces).strip()
        if value:
            kept.append(value)
    return MARKER.join(kept)


def instruction_revision(task, instruction, at, reason, note=''):
    history = list(task.get('instruction_history') or [])
    history.append({'at': at, 'attempt': task.get('attempt', 0),
                    'reason': reason, 'instruction': task['instruction'],
                    'submitted_note': str(note)})
    return {'instruction': instruction, 'instruction_history': history}
