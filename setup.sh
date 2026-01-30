#!/bin/bash
set -e

APP_DIR="/root/sapdaplaywright"

echo "➡️ Creating virtual environment..."
python3 -m venv venv

echo "➡️ Activating venv..."
source venv/bin/activate

echo "➡️ Upgrading pip..."
pip install --upgrade pip

echo "➡️ Installing requirements..."
pip install -r requirements.txt

echo "✅ Python environment ready."
