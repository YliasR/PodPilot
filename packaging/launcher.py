"""Entry point for the frozen (PyInstaller) build."""

import sys

from podpilot.gui import main

sys.exit(main())
