$script:MinerUProjectRoot = Split-Path -Parent $PSScriptRoot
$script:MinerURuntimeVariableNames = @(
    "CONDARC",
    "CONDA_PKGS_DIRS",
    "PIP_CACHE_DIR",
    "UV_CACHE_DIR",
    "XDG_CACHE_HOME",
    "HF_HOME",
    "HUGGINGFACE_HUB_CACHE",
    "MODELSCOPE_CACHE",
    "TORCH_HOME",
    "CUDA_PATH",
    "MPLCONFIGDIR",
    "NUMBA_CACHE_DIR",
    "PYINSTALLER_CONFIG_DIR",
    "MINERU_TOOLS_CONFIG_JSON",
    "MINERU_MODEL_SOURCE",
    "MINERU_LOCAL_ROOT",
    "PYTHONDONTWRITEBYTECODE",
    "TEMP",
    "TMP"
)

function Get-MinerURuntimePaths {
    $root = $script:MinerUProjectRoot
    $runtime = Join-Path $root "runtime"
    $models = Join-Path $root "models"
    [pscustomobject]@{
        ProjectRoot = $root
        Runtime = $runtime
        Environment = Join-Path $runtime "env"
        CondaPackages = Join-Path $runtime "conda-pkgs"
        Cache = Join-Path $runtime "cache"
        Cuda = Join-Path $runtime "cuda"
        Temp = Join-Path $runtime "temp"
        MinerUConfig = Join-Path $runtime "mineru.json"
        ConfigTemplate = Join-Path $root "config\mineru.example.json"
        Condarc = Join-Path $root "config\condarc"
        Models = $models
        ModelScope = Join-Path $models "modelscope"
        HuggingFace = Join-Path $models "huggingface"
    }
}

function Initialize-MinerURuntime {
    param([switch]$CreateConfig)

    $paths = Get-MinerURuntimePaths
    $directories = @(
        $paths.Runtime,
        $paths.CondaPackages,
        $paths.Cache,
        $paths.Cuda,
        $paths.Temp,
        $paths.Models,
        $paths.ModelScope,
        $paths.HuggingFace,
        (Join-Path $paths.Cache "pip"),
        (Join-Path $paths.Cache "uv"),
        (Join-Path $paths.Cache "torch"),
        (Join-Path $paths.Cache "matplotlib"),
        (Join-Path $paths.Cache "numba"),
        (Join-Path $paths.Cache "pyinstaller")
    )
    foreach ($directory in $directories) {
        New-Item -ItemType Directory -Force -Path $directory | Out-Null
    }

    if ($CreateConfig -and -not (Test-Path $paths.MinerUConfig)) {
        if (-not (Test-Path $paths.ConfigTemplate)) {
            throw "MinerU config template is missing: $($paths.ConfigTemplate)"
        }
        Copy-Item -LiteralPath $paths.ConfigTemplate -Destination $paths.MinerUConfig
    }

    $env:CONDARC = $paths.Condarc
    $env:CONDA_PKGS_DIRS = $paths.CondaPackages
    $env:PIP_CACHE_DIR = Join-Path $paths.Cache "pip"
    $env:UV_CACHE_DIR = Join-Path $paths.Cache "uv"
    $env:XDG_CACHE_HOME = $paths.Cache
    $env:HF_HOME = $paths.HuggingFace
    $env:HUGGINGFACE_HUB_CACHE = Join-Path $paths.HuggingFace "hub"
    $env:MODELSCOPE_CACHE = $paths.ModelScope
    $env:TORCH_HOME = Join-Path $paths.Cache "torch"
    $env:CUDA_PATH = $paths.Cuda
    $env:MPLCONFIGDIR = Join-Path $paths.Cache "matplotlib"
    $env:NUMBA_CACHE_DIR = Join-Path $paths.Cache "numba"
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $paths.Cache "pyinstaller"
    $env:MINERU_TOOLS_CONFIG_JSON = $paths.MinerUConfig
    $env:MINERU_MODEL_SOURCE = "local"
    $env:MINERU_LOCAL_ROOT = $paths.ProjectRoot
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $env:TEMP = $paths.Temp
    $env:TMP = $paths.Temp
    return $paths
}
