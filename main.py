"""
SiteSnatcher — entry point.

Run:  python main.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ui.app import run

if __name__ == "__main__":
    run()
