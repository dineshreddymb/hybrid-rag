import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

# Unit tests never touch real services or APIs.
os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("EMBEDDING_PROVIDER", "fake")
os.environ.setdefault("LOG_TO_MONGO", "false")
os.environ.setdefault("LANGFUSE_PUBLIC_KEY", "")
os.environ.setdefault("LANGFUSE_SECRET_KEY", "")
os.environ.setdefault("FAST_MODE", "false")
os.environ.setdefault("ENABLE_GRADING", "true")
os.environ.setdefault("ENABLE_GROUNDING_CHECK", "true")

DOCS = ROOT / "data" / "docs"
