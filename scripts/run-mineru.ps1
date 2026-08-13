param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$InputPath,

    [Parameter(Position = 1)]
    [string]$OutputPath = "",

    [ValidateSet("pipeline", "vlm-engine", "hybrid-engine", "vlm-http-client", "hybrid-http-client")]
    [string]$Backend = "hybrid-engine",

    [ValidateSet("medium", "high")]
    [string]$Effort = "medium"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "activate.ps1")

if (-not [System.IO.Path]::IsPathRooted($InputPath)) {
    $InputPath = Join-Path $ProjectRoot $InputPath
}
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $ProjectRoot "output"
} elseif (-not [System.IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath = Join-Path $ProjectRoot $OutputPath
}

mineru -p $InputPath -o $OutputPath -b $Backend --effort $Effort
$MineruExitCode = $LASTEXITCODE
if ($MineruExitCode -ne 0) {
    exit $MineruExitCode
}
