#!/bin/bash
# Exit on error
set -e

# Install Python 3.10 if not already installed
if ! command -v python3.10 &> /dev/null; then
    echo "Installing Python 3.10..."
    apt-get update && apt-get install -y python3.10 python3.10-venv
fi

# Create and activate virtual environment
python3.10 -m venv venv
source venv/bin/activate

# Upgrade pip
pip install --upgrade pip

# Install dependencies
pip install -r requirements.txt

# Create a .python-version file for Render
python --version > .python-version
