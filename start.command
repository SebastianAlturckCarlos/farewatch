#!/bin/bash
cd "$(dirname "$0")"
python3 -m pip install --quiet -r requirements.txt
python3 app.py
