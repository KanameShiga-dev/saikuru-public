"""Lossless reviewer handoff deduplication; evaluation-gated by team_config."""
import json
from pathlib import Path


def evaluation_mode_for_job(config, job):
    """Keep normal jobs on the original context even if the evaluation flag is set."""
    mode = config.get('review_context_mode', 'off')
    route = config.get('routing_evaluation') or {}
    if mode not in ('shadow', 'on') or route.get('enabled') is not True:
        return 'off'
    if (job.get('evaluation') or {}).get('mode') not in ('A', 'B', 'C', 'D'):
        return 'off'
    try:
        if not Path(job['project']).resolve().is_relative_to(Path(route['root']).resolve()):
            return 'off'
    except (KeyError, OSError, TypeError, ValueError):
        return 'off'
    return mode


def without_own_instruction(package, task_id, instruction):
    """Omit only the reviewer's own instruction, which is already in the prompt."""
    instructions = package.get('latest_task_instructions') or {}
    own = instructions.get(task_id)
    if not isinstance(own, dict) or own.get('instruction') != instruction:
        return package, 0
    compact = dict(package)
    compact['latest_task_instructions'] = dict(instructions)
    compact['latest_task_instructions'][task_id] = {key: value for key, value in own.items()
                                                     if key != 'instruction'}
    removed = len(instruction)
    return compact, removed
