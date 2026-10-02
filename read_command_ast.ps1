# Parse only. Never invoke or evaluate the supplied command.
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.Encoding]::UTF8
[Console]::OutputEncoding = [Text.Encoding]::UTF8
try {
    $source = [Console]::In.ReadToEnd()
    $tokens = $null; $parseErrors = $null
    $ast = [Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count -or $ast.ParamBlock -or $ast.BeginBlock -or $ast.ProcessBlock -or $ast.DynamicParamBlock -or $ast.UsingStatements.Count -or $ast.ScriptRequirements -or $ast.EndBlock.Traps.Count) { throw 'Unsupported' }
    $rows = @()
    foreach ($statement in $ast.EndBlock.Statements) {
        if ($statement -isnot [Management.Automation.Language.PipelineAst]) { throw 'Statement' }
        $pipeline = @()
        foreach ($command in $statement.PipelineElements) {
            if ($command -isnot [Management.Automation.Language.CommandAst] -or $command.Redirections.Count -or $command.InvocationOperator -ne 'Unknown') { throw 'Command' }
            $parts = @()
            foreach ($element in $command.CommandElements) {
                if ($element -is [Management.Automation.Language.StringConstantExpressionAst]) { $parts += $element.Value }
                elseif ($element -is [Management.Automation.Language.ExpandableStringExpressionAst] -and $element.NestedExpressions.Count -eq 0) { $parts += $element.Value }
                elseif ($element -is [Management.Automation.Language.ConstantExpressionAst] -and $element.Value -is [int]) { $parts += [string]$element.Value }
                elseif ($element -is [Management.Automation.Language.CommandParameterAst] -and $null -eq $element.Argument) { $parts += '-' + $element.ParameterName }
                else { throw 'Dynamic expression' }
            }
            $pipeline += ,$parts
        }
        $rows += ,$pipeline
    }
    ConvertTo-Json -InputObject @{pipelines=$rows} -Depth 8 -Compress
} catch { '{"pipelines":[]}' }
