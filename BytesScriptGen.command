#!/bin/bash
# ═══════════════════════════════════════════
#  Bytes ScriptGen — Double-click to run!
# ═══════════════════════════════════════════

cd "$(dirname "$0")"

echo ""
echo "  ┌─────────────────────────────────┐"
echo "  │   Bytes ScriptGen              │"
echo "  │   Starting up...                │"
echo "  └─────────────────────────────────┘"
echo ""

# Check if Python 3 is installed
if ! command -v python3 &>/dev/null; then
    echo "❌ Python 3 is required but not installed."
    echo "   Install it from: https://www.python.org/downloads/"
    echo ""
    echo "   Press any key to exit..."
    read -n 1
    exit 1
fi

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "📦 First run — setting up (one time only)..."
    python3 -m venv venv
    source venv/bin/activate
    pip install -q -r requirements.txt
    echo "✅ Setup complete!"
    echo ""
else
    source venv/bin/activate
fi

# Check for custom API key
if [ -f ".env" ]; then
    export $(cat .env | grep -v '^#' | xargs)
fi

echo "🚀 Starting server..."
echo "   Browser will open automatically."
echo "   Press Ctrl+C to stop."
echo ""

python3 app.py
