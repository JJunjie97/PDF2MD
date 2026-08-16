from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPT = ROOT / "skills" / "pdf2md-read-pdf" / "scripts" / "pdf2md_pdf.py"


def load_skill_module():
    spec = importlib.util.spec_from_file_location("pdf2md_skill_runtime_test", SKILL_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load skill script: {SKILL_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


skill = load_skill_module()


class SkillRuntimeDiscoveryTests(unittest.TestCase):
    def make_runtime_root(self, directory: str) -> Path:
        root = Path(directory)
        python = root / "runtime" / "env" / "python.exe"
        cli = root / "src" / "pdf2md_cli.py"
        python.parent.mkdir(parents=True)
        cli.parent.mkdir(parents=True)
        python.touch()
        cli.touch()
        return root

    def test_public_root_environment_variable_has_priority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_runtime_root(directory)
            with patch.dict(os.environ, {"PDF2MD_ROOT": str(root), "PDF2MD_AGENT_ROOT": ""}):
                runtime = skill.runtime()
            self.assertEqual(runtime.root, root.resolve())

    def test_wrapper_discovered_root_is_accepted_for_copied_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_runtime_root(directory)
            with patch.dict(os.environ, {"PDF2MD_ROOT": "", "PDF2MD_AGENT_ROOT": str(root)}):
                runtime = skill.runtime()
            self.assertEqual(runtime.root, root.resolve())


if __name__ == "__main__":
    unittest.main()
