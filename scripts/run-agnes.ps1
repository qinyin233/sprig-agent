# Agnes preset for native PowerShell. Credentials stay in this process environment.
$ErrorActionPreference = 'Stop'
$sprigRoot = Split-Path -Parent $PSScriptRoot
$sprigUv = (Get-Command uv -ErrorAction Stop).Source
$sprigArgs = @($args)
if ($sprigArgs.Count -eq 0) { $sprigArgs = @('--tui') }
$sprigExitCode = 1
$sprigPrevious = @{}
foreach ($sprigName in @('SPRIG_MODEL', 'SPRIG_BASE_URL', 'SPRIG_API_KEY', 'PYTHONUTF8')) {
    $sprigPrevious[$sprigName] = [Environment]::GetEnvironmentVariable($sprigName, 'Process')
}
try {
    $env:SPRIG_MODEL = 'agnes-3.0-flash'
    $env:SPRIG_BASE_URL = 'https://apihub.agnes-ai.com/v1'
    $env:PYTHONUTF8 = '1'
    $sprigHelpOnly = ($sprigArgs -contains '--help') -or ($sprigArgs -contains '-h') -or ($sprigArgs -contains '--version')
    if (-not $sprigHelpOnly -and -not $env:SPRIG_API_KEY -and -not $env:OPENAI_API_KEY -and -not $env:AGNES_API_KEY) {
        $sprigSecureKey = Read-Host 'Agnes API key (hidden)' -AsSecureString
        $sprigKeyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sprigSecureKey)
        try { $env:SPRIG_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($sprigKeyPointer) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($sprigKeyPointer); $sprigSecureKey.Dispose() }
    }
    & $sprigUv run --project $sprigRoot --locked --managed-python python -m sprig_agent @sprigArgs
    $sprigExitCode = $LASTEXITCODE
}
finally {
    foreach ($sprigName in $sprigPrevious.Keys) {
        [Environment]::SetEnvironmentVariable($sprigName, $sprigPrevious[$sprigName], 'Process')
    }
}
exit $sprigExitCode
