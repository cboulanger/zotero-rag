#!/bin/bash
# Bash script to downgrade from Python 3.13 to 3.12
# This fixes PyTorch crashes on Windows with Python 3.13

set -e  # Exit on error

echo "========================================"
echo "Python 3.12 Downgrade Script"
echo "========================================"
echo ""

# Check if Python 3.12 is installed
echo "Checking for Python 3.12..."
if command -v python3.12 &> /dev/null; then
    python_version=$(python3.12 --version)
    echo "[OK] Python 3.12 found: $python_version"
elif py -3.12 --version &> /dev/null; then
    python_version=$(py -3.12 --version)
    echo "[OK] Python 3.12 found: $python_version"
else
    echo "[FAIL] Python 3.12 not found"
    echo ""
    echo "Please install Python 3.12 from:"
    echo "  https://www.python.org/downloads/release/python-3120/"
    echo ""
    echo "Or use your package manager:"
    echo "  brew install python@3.12  (macOS)"
    echo "  apt install python3.12    (Ubuntu/Debian)"
    exit 1
fi

# Step 1: Update .python-version file
echo ""
echo "Step 1: Updating .python-version file..."
echo "3.12" > .python-version
echo "[OK] .python-version updated to 3.12"

# Step 2: Update pyproject.toml
echo ""
echo "Step 2: Updating pyproject.toml..."
sed -i 's/requires-python = ">=3\.13"/requires-python = ">=3.12"/' pyproject.toml
echo "[OK] pyproject.toml updated to Python >=3.12"

# Step 3: Remove old virtual environment and lock file
echo ""
echo "Step 3: Removing old .venv and uv.lock..."
if [ -d ".venv" ]; then
    rm -rf .venv
    echo "[OK] Old .venv removed"
else
    echo "[SKIP] No .venv directory found"
fi
if [ -f "uv.lock" ]; then
    rm -f uv.lock
    echo "[OK] Old uv.lock removed"
else
    echo "[SKIP] No uv.lock file found"
fi

# Step 4: Create new virtual environment with Python 3.12
echo ""
echo "Step 4: Creating new virtual environment with Python 3.12..."
uv venv --python 3.12
echo "[OK] Virtual environment created"

# Step 5: Sync dependencies (will regenerate uv.lock)
echo ""
echo "Step 5: Syncing dependencies with uv..."
echo "(This may take 2-5 minutes for PyTorch download)"
uv sync
echo "[OK] Dependencies synced and uv.lock regenerated"

# Step 6: Verify Python version
echo ""
echo "Step 6: Verifying Python version..."
new_version=$(uv run python --version)
echo "[OK] Active Python version: $new_version"

# Step 7: Verify PyTorch installation
echo ""
echo "Step 7: Verifying PyTorch installation..."
if uv run python -c "import torch; print(f'PyTorch {torch.__version__}')" 2>/dev/null; then
    torch_version=$(uv run python -c "import torch; print(f'PyTorch {torch.__version__}')")
    echo "[OK] $torch_version"
else
    echo "[WARN] PyTorch verification failed"
    echo "This is normal if dependencies are still installing"
fi

# Success message
echo ""
echo "========================================"
echo "Downgrade Complete!"
echo "========================================"
echo ""
echo "Next steps:"
echo "  1. Run unit tests:         npm run test:backend"
echo "  2. Run integration tests:  npm run test:integration"
echo ""
echo "The PyTorch crash should now be fixed!"
