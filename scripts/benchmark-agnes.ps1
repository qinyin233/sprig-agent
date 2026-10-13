# Diagnostic task runner. The API key stays in this process and its uv child.
$ErrorActionPreference = 'Stop'
$sprigBenchmarkRoot = Split-Path -Parent $PSScriptRoot
$sprigBenchmarkUv = (Get-Command uv -ErrorAction Stop).Source
$sprigBenchmarkArgs = @($args)
$sprigBenchmarkClient = 'sprig'
$sprigBenchmarkClientSelected = $false
$sprigBenchmarkFiltered = @()
for ($sprigBenchmarkIndex = 0; $sprigBenchmarkIndex -lt $sprigBenchmarkArgs.Count; $sprigBenchmarkIndex++) {
    if ($sprigBenchmarkArgs[$sprigBenchmarkIndex] -eq '--client') {
        if ($sprigBenchmarkClientSelected -or $sprigBenchmarkIndex + 1 -ge $sprigBenchmarkArgs.Count -or
            $sprigBenchmarkArgs[$sprigBenchmarkIndex + 1] -notin @('sprig', 'mini')) {
            throw '--client requires sprig or mini; only one client selection is accepted'
        }
        $sprigBenchmarkIndex++
        $sprigBenchmarkClient = $sprigBenchmarkArgs[$sprigBenchmarkIndex]
        $sprigBenchmarkClientSelected = $true
    } else {
        $sprigBenchmarkFiltered += $sprigBenchmarkArgs[$sprigBenchmarkIndex]
    }
}
$sprigBenchmarkArgs = $sprigBenchmarkFiltered
if ($sprigBenchmarkArgs.Count -eq 0) { $sprigBenchmarkArgs = @('--list') }
$sprigBenchmarkProject = $sprigBenchmarkRoot
$sprigBenchmarkDriver = Join-Path $PSScriptRoot 'benchmark-tasks.py'
if ($sprigBenchmarkClient -eq 'mini') {
    $sprigBenchmarkProject = Join-Path $sprigBenchmarkRoot 'benchmarks\clients\mini'
    $sprigBenchmarkDriver = Join-Path $sprigBenchmarkRoot 'benchmarks\clients\mini_task.py'
}
$sprigBenchmarkExit = 1
$sprigBenchmarkPrevious = @{}
foreach ($sprigBenchmarkName in @('SPRIG_MODEL', 'SPRIG_BASE_URL', 'SPRIG_API_KEY', 'PYTHONUTF8')) {
    $sprigBenchmarkPrevious[$sprigBenchmarkName] = [Environment]::GetEnvironmentVariable($sprigBenchmarkName, 'Process')
}
try {
    $env:SPRIG_MODEL = 'agnes-3.0-flash'
    $env:SPRIG_BASE_URL = 'https://apihub.agnes-ai.com/v1'
    $env:PYTHONUTF8 = '1'
    $sprigBenchmarkHelp = ($sprigBenchmarkArgs -contains '--help') -or ($sprigBenchmarkArgs -contains '-h')
    if (($sprigBenchmarkArgs -contains '--run') -and -not $sprigBenchmarkHelp -and -not $env:SPRIG_API_KEY -and -not $env:OPENAI_API_KEY -and -not $env:AGNES_API_KEY) {
        $sprigBenchmarkSecure = Read-Host 'Agnes API key (hidden; process only)' -AsSecureString
        $sprigBenchmarkPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sprigBenchmarkSecure)
        try { $env:SPRIG_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($sprigBenchmarkPointer) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($sprigBenchmarkPointer); $sprigBenchmarkSecure.Dispose() }
    }
    & $sprigBenchmarkUv run --project $sprigBenchmarkProject --locked --managed-python python $sprigBenchmarkDriver @sprigBenchmarkArgs
    $sprigBenchmarkExit = $LASTEXITCODE
}
finally {
    foreach ($sprigBenchmarkName in $sprigBenchmarkPrevious.Keys) {
        [Environment]::SetEnvironmentVariable($sprigBenchmarkName, $sprigBenchmarkPrevious[$sprigBenchmarkName], 'Process')
    }
}
exit $sprigBenchmarkExit
