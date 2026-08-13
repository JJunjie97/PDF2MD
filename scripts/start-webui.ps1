param(
    [int]$Port = 7860,
    [string]$HostAddress = "127.0.0.1"
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "activate.ps1")
mineru-gradio --server-name $HostAddress --server-port $Port
