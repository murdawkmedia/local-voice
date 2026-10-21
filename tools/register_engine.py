#!/usr/bin/env python3
"""Register an explicitly verified, locally built result-json engine."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_voice.engine_selection import register_engine

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--engine', required=True, choices=['whisper', 'parakeet'])
parser.add_argument('--binary', required=True, type=Path)
parser.add_argument('--sha256', required=True)
parser.add_argument('--app-dir', type=Path, default=Path.home()/'.local/opt/local-voice')
parser.add_argument('--vulkan-icd', help='Optional administrator-installed Vulkan ICD JSON; omit for CPU')
args = parser.parse_args()
register_engine(args.app_dir, args.engine, args.binary, args.sha256, args.vulkan_icd)
print('Verified local engine registered. No model was downloaded or microphone opened.')
