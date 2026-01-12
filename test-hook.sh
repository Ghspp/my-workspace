#!/bin/sh
# Test script to run the pre-commit hook manually

cd "$(dirname "$0")"
echo "Testing pre-commit hook..."
echo "Current directory: $(pwd)"
echo ""
echo "Running hook..."
echo "=========================================="
.git/hooks/pre-commit
echo "=========================================="
echo ""
echo "Hook finished with exit code: $?"

