"""The adapter modules import each other flat (`import derive`), so the tests
put adapter/ itself on the path the same way serve.py does."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapter"))
