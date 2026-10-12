"""Privacy-minimal reviewer prompt measurements for isolated sample manifests."""


def reviewer_prompts(store, job_id):
    result = []
    for task in store.all('task'):
        if task['job_id'] != job_id or task['role'] != 'reviewer':
            continue
        run = task.get('agent_run') or {}
        size = run.get('context_size') or {}
        refs = run.get('context_refs') or {}
        reduction = refs.get('review_context') or {}
        result.append({'reviewer': task.get('agent_name', 'reviewer'),
                       'status': task['status'],
                       'user_prompt_chars': size.get('user_prompt_chars'),
                       'handoff_context_chars': refs.get('context_chars'),
                       'mode': reduction.get('mode', 'off'),
                       'duplicate_instruction_chars': reduction.get('duplicate_instruction_chars')})
    return result
