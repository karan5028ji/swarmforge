#!/usr/bin/env python3
"""Backward-compatible shim: `python forge.py "..."` still works."""
import sys

from swarmforge import main

if __name__ == "__main__":
    sys.exit(main())
