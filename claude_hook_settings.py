"""Build the shared Claude egress-hook configuration used by jobs and E2E."""


def build_egress_hook_settings(command, post_tool_use_args,
                              post_tool_use_failure_args, post_tool_batch_args):
    """Return hook declarations with the same failure policy for every caller.

    PostToolUse events cannot undo a tool action. PostToolBatch is the gate before
    the next model request, so hook failures there must block.
    """
    def command_hook(args, timeout, on_failure=None):
        hook = {'type': 'command', 'command': command, 'args': list(args),
                'timeout': timeout}
        if on_failure is not None:
            hook['onFailure'] = on_failure
        return hook

    return {'hooks': {
        'PostToolUse': [{'matcher': '*', 'hooks': [
            command_hook(post_tool_use_args, 40)]}],
        'PostToolUseFailure': [{'matcher': '*', 'hooks': [
            command_hook(post_tool_use_failure_args, 40)]}],
        'PostToolBatch': [{'hooks': [
            command_hook(post_tool_batch_args, 30, 'block')]}],
    }}


def validate_egress_hook_settings(settings, batch_only=False):
    """Reject incomplete or weakened egress settings before starting Claude."""
    hooks = settings.get('hooks') if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        raise ValueError('egress_hooks_missing')
    if not hooks:
        raise ValueError('egress_hooks_missing')
    expected = {
        'PostToolUse': ('*', None),
        'PostToolUseFailure': ('*', None),
        'PostToolBatch': (None, 'block'),
    }
    events = {'PostToolBatch': expected['PostToolBatch']} if batch_only else expected
    for event, (matcher, on_failure) in events.items():
        groups = hooks.get(event)
        if not isinstance(groups, list) or len(groups) != 1:
            raise ValueError('egress_hook_event_missing:' + event)
        group = groups[0]
        if matcher is None:
            if 'matcher' in group:
                raise ValueError('egress_hook_matcher_invalid:' + event)
        elif group.get('matcher') != matcher:
            raise ValueError('egress_hook_matcher_invalid:' + event)
        commands = group.get('hooks')
        if not isinstance(commands, list) or len(commands) != 1:
            raise ValueError('egress_hook_command_missing:' + event)
        hook = commands[0]
        if (hook.get('type') != 'command' or not isinstance(hook.get('command'), str)
                or not hook['command'] or not isinstance(hook.get('args'), list)
                or not hook['args'] or not isinstance(hook.get('timeout'), int)
                or hook['timeout'] <= 0):
            raise ValueError('egress_hook_command_invalid:' + event)
        if hook.get('onFailure') != on_failure:
            raise ValueError('egress_hook_failure_policy_invalid:' + event)
    return True
