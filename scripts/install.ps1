$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "runtime.ps1")

$Paths = Initialize-PDF2MDRuntime -CreateConfig
if (-not (Test-Path (Join-Path $Paths.Environment "python.exe"))) {
    Write-Host "Creating local Python 3.12 environment..." -ForegroundColor Cyan
    conda create --prefix $Paths.Environment --yes --no-default-packages --override-channels --channel conda-forge python=3.12 pip
    if ($LASTEXITCODE -ne 0) {
        throw "Conda environment creation failed with exit code $LASTEXITCODE."
    }
}

$CondaEnvironmentVariables = foreach ($name in $script:PDF2MDRuntimeVariableNames) {
    "$name=$([Environment]::GetEnvironmentVariable($name, 'Process'))"
}
conda env config vars set --prefix $Paths.Environment @CondaEnvironmentVariables | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Persisting local environment paths failed with exit code $LASTEXITCODE."
}

$Python = Join-Path $Paths.Environment "python.exe"
Write-Host "Installing PDF2MD OCR components..." -ForegroundColor Cyan
& $Python -m pip install --upgrade pip uv
if ($LASTEXITCODE -ne 0) {
    throw "pip/uv installation failed with exit code $LASTEXITCODE."
}
& $Python -m uv pip install --python $Python --upgrade "mineru[vlm,pipeline,lmdeploy]==3.4.4" "requests>=2.32,<3" "pypdf>=5,<7" "pyinstaller>=6,<7"
if ($LASTEXITCODE -ne 0) {
    throw "PDF2MD engine installation failed with exit code $LASTEXITCODE."
}

$NvidiaGpu = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($NvidiaGpu) {
    $CudaAvailable = & $Python -c "import torch; print(str(torch.cuda.is_available()).lower())"
    if ($CudaAvailable -ne "true") {
        Write-Host "NVIDIA GPU detected; installing CUDA 12.8 PyTorch wheels..." -ForegroundColor Cyan
        & $Python -m pip install --upgrade "torch==2.8.0+cu128" "torchvision==0.23.0+cu128" --index-url https://download.pytorch.org/whl/cu128
        if ($LASTEXITCODE -ne 0) {
            throw "CUDA PyTorch installation failed with exit code $LASTEXITCODE."
        }
    }
}

# This project has no web UI. Remove packages left by an older mineru[all]
# installation while retaining FastAPI, which is used as the local OCR engine.
& $Python -m pip uninstall --yes gradio gradio-pdf gradio-client 2>$null | Out-Null
$LegacyWebLauncher = Join-Path $Paths.Environment "Scripts\mineru-gradio.exe"
if (Test-Path $LegacyWebLauncher) {
    Remove-Item -LiteralPath $LegacyWebLauncher -Force
}
$LegacyGradioPackage = Join-Path $Paths.Environment "Lib\site-packages\gradio"
if (Test-Path $LegacyGradioPackage) {
    Remove-Item -LiteralPath $LegacyGradioPackage -Recurse -Force
}

$TorchLibraryPath = Join-Path $Paths.Environment "Lib\site-packages\torch\lib"
$CudaBinPath = Join-Path $Paths.Cuda "bin"
if ((Test-Path $TorchLibraryPath) -and -not (Test-Path $CudaBinPath)) {
    New-Item -ItemType Junction -Path $CudaBinPath -Target $TorchLibraryPath | Out-Null
}

Write-Host "Verifying installation..." -ForegroundColor Cyan
& $Python -c "import mineru, torch; print('PDF2MD engine:', getattr(mineru, '__version__', 'installed')); print('PyTorch:', torch.__version__); print('CUDA available:', torch.cuda.is_available()); print('CUDA runtime:', torch.version.cuda); print('GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU only')"
& $Python -m mineru.cli.client --version

Write-Host "Installation complete. Download models with: .\scripts\download-models.ps1" -ForegroundColor Green
