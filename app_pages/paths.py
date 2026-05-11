"""Project paths and sys.path bootstrap for netsuite_core (import early from app)."""
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NETSUITE_DIR = os.path.join(BASE_DIR, "netsuite_core")
if NETSUITE_DIR not in sys.path:
    sys.path.insert(0, NETSUITE_DIR)
