# PowerShell script to downgrade from Python 3.13 to 3.12
# This fixes PyTorch crashes on Windows with Python 3.13

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Python 3.12 Downgrade Script" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Check if Python 3.12 is installed
Write-Host "Checking for Python 3.12..." -ForegroundColor Yellow
$python312 = $null
try {
    $python312 = py -3.12 --version 2>&1
    Write-Host "[OK] Python 3.12 found: $python312" -ForegroundColor Green
} catch {
    Write-Host "[FAIL] Python 3.12 not found" -ForegroundColor Red
    Write-Host ""
    Write-Host "Please install Python 3.12 from:" -ForegroundColor Yellow
    Write-Host "  https://www.python.org/downloads/release/python-3120/" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "Or use winget:" -ForegroundColor Yellow
    Write-Host "  winget install Python.Python.3.12" -ForegroundColor Cyan
    exit 1
}

# Step 1: Update .python-version file
Write-Host ""
Write-Host "Step 1: Updating .python-version file..." -ForegroundColor Yellow
Set-Content -Path ".python-version" -Value "3.12"
Write-Host "[OK] .python-version updated to 3.12" -ForegroundColor Green

# Step 2: Update pyproject.toml
Write-Host ""
Write-Host "Step 2: Updating pyproject.toml..." -ForegroundColor Yellow
$pyprojectContent = Get-Content -Path "pyproject.toml" -Raw
$pyprojectContent = $pyprojectContent -replace 'requires-python = ">=3\.13"', 'requires-python = ">=3.12"'
Set-Content -Path "pyproject.toml" -Value $pyprojectContent
Write-Host "[OK] pyproject.toml updated to Python >=3.12" -ForegroundColor Green

# Step 3: Remove old virtual environment and lock file
Write-Host ""
Write-Host "Step 3: Removing old .venv and uv.lock..." -ForegroundColor Yellow
if (Test-Path ".venv") {
    Remove-Item -Recurse -Force .venv
    Write-Host "[OK] Old .venv removed" -ForegroundColor Green
} else {
    Write-Host "[SKIP] No .venv directory found" -ForegroundColor Gray
}
if (Test-Path "uv.lock") {
    Remove-Item -Force uv.lock
    Write-Host "[OK] Old uv.lock removed" -ForegroundColor Green
} else {
    Write-Host "[SKIP] No uv.lock file found" -ForegroundColor Gray
}

# Step 4: Create new virtual environment with Python 3.12
Write-Host ""
Write-Host "Step 4: Creating new virtual environment with Python 3.12..." -ForegroundColor Yellow
try {
    uv venv --python 3.12
    Write-Host "[OK] Virtual environment created" -ForegroundColor Green
} catch {
    Write-Host "[FAIL] Failed to create virtual environment" -ForegroundColor Red
    Write-Host "Error: $_" -ForegroundColor Red
    exit 1
}

# Step 5: Sync dependencies (will regenerate uv.lock)
Write-Host ""
Write-Host "Step 5: Syncing dependencies with uv..." -ForegroundColor Yellow
Write-Host "(This may take 2-5 minutes for PyTorch download)" -ForegroundColor Gray
try {
    uv sync
    Write-Host "[OK] Dependencies synced and uv.lock regenerated" -ForegroundColor Green
} catch {
    Write-Host "[FAIL] Failed to sync dependencies" -ForegroundColor Red
    Write-Host "Error: $_" -ForegroundColor Red
    exit 1
}

# Step 6: Verify Python version
Write-Host ""
Write-Host "Step 6: Verifying Python version..." -ForegroundColor Yellow
$newVersion = uv run python --version
Write-Host "[OK] Active Python version: $newVersion" -ForegroundColor Green

# Step 7: Verify PyTorch installation
Write-Host ""
Write-Host "Step 7: Verifying PyTorch installation..." -ForegroundColor Yellow
try {
    $torchVersion = uv run python -c "import torch; print(f'PyTorch {torch.__version__}')"
    Write-Host "[OK] $torchVersion" -ForegroundColor Green
} catch {
    Write-Host "[WARN] PyTorch verification failed" -ForegroundColor Yellow
    Write-Host "This is normal if dependencies are still installing" -ForegroundColor Gray
}

# Success message
Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "Downgrade Complete!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. Run unit tests:         npm run test:backend" -ForegroundColor White
Write-Host "  2. Run integration tests:  npm run test:integration" -ForegroundColor White
Write-Host ""
Write-Host "The PyTorch crash should now be fixed!" -ForegroundColor Green
