"""Suppress an exact retry of the latest supplement; preserve instruction content."""


MARKER = '\n利用者の補足: '


def compact_instruction(instruction, note=''):
    text, supplement = str(instruction), str(note)
    if not supplement.strip():
        return text
    # Never normalize whitespace or deduplicate lines/sentences: indentation,
    # table rows, scoped requirements and later reversals can all be significant.
    # A nonconsecutive repeat may intentionally restore an earlier decision.
    if MARKER in text and text.rsplit(MARKER, 1)[1] == supplement:
        return text
    return text + MARKER + supplement


def instruction_revision(task, instruction, at, reason, note=''):
    history = list(task.get('instruction_history') or [])
    history.append({'at': at, 'attempt': task.get('attempt', 0),
                    'reason': reason, 'instruction': task['instruction'],
                    'submitted_note': str(note)})
    return {'instruction': instruction, 'instruction_history': history}
