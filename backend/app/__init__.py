"""Aplicación de extracción."""

from pathlib import Path
import sys

CONTRACTS_DIR = Path(__file__).resolve().parents[4] / "contracts"
if str(CONTRACTS_DIR) not in sys.path:
    sys.path.insert(0, str(CONTRACTS_DIR))
