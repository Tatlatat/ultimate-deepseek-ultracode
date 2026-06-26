#!/usr/bin/env python3
"""Compatibility shim: the gateway now lives in the reasonix_gateway package.
This path is kept stable because the launcher, MCP, install.sh, and the test suite
import from here. Everything is re-exported from reasonix_gateway."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reasonix_gateway import *  # noqa: F401,F403
from reasonix_gateway.server import main

if __name__ == "__main__":
    sys.exit(main())
