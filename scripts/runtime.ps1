$script:PDF2MDProjectRoot = Split-Path -Parent $PSScriptRoot
$script:PDF2MDRuntimeVariableNames = @(
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
    "PDF2MD_ROOT",
    "PYTHONDONTWRITEBYTECODE",
    "TEMP",
    "TMP"
)

function Get-PDF2MDRuntimePaths {
    $root = $script:PDF2MDProjectRoot
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
        EngineConfig = Join-Path $runtime "pdf2md.json"
        ConfigTemplate = Join-Path $root "config\pdf2md.example.json"
        Condarc = Join-Path $root "config\condarc"
        Models = $models
        ModelScope = Join-Path $models "modelscope"
        HuggingFace = Join-Path $models "huggingface"
    }
}

function Initialize-PDF2MDRuntime {
    param([switch]$CreateConfig)

    $paths = Get-PDF2MDRuntimePaths
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

    $LegacyEngineConfig = Join-Path $paths.Runtime "mineru.json"
    if (-not (Test-Path $paths.EngineConfig) -and (Test-Path $LegacyEngineConfig)) {
        Move-Item -LiteralPath $LegacyEngineConfig -Destination $paths.EngineConfig
    }

    if ($CreateConfig -and -not (Test-Path $paths.EngineConfig)) {
        if (-not (Test-Path $paths.ConfigTemplate)) {
            throw "PDF2MD config template is missing: $($paths.ConfigTemplate)"
        }
        Copy-Item -LiteralPath $paths.ConfigTemplate -Destination $paths.EngineConfig
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
    $env:MINERU_TOOLS_CONFIG_JSON = $paths.EngineConfig
    $env:MINERU_MODEL_SOURCE = "local"
    $env:PDF2MD_ROOT = $paths.ProjectRoot
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $env:TEMP = $paths.Temp
    $env:TMP = $paths.Temp
    return $paths
}
