import tempfile
import unittest
from pathlib import Path

from poc import config, providers


class ParseEnvFileTest(unittest.TestCase):
    def write(self, text: str) -> Path:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8")
        tmp.write(text)
        tmp.close()
        self.addCleanup(Path(tmp.name).unlink)
        return Path(tmp.name)

    def test_formats(self):
        path = self.write(
            "# 注释\n\nA=1\nexport B = two\nC=\"quoted # not comment\"\nD='single'\nE=val # trailing\nbroken line\n=nokey\nF=\n"
        )
        self.assertEqual(
            config.parse_env_file(path),
            {"A": "1", "B": "two", "C": "quoted # not comment", "D": "single", "E": "val", "F": ""},
        )

    def test_process_env_wins(self):
        path = self.write("ANTHROPIC_API_KEY=from-file\nDEEPSEEK_API_KEY=from-file\n")
        environ = {"ANTHROPIC_API_KEY": "from-process"}
        loaded = config.load_env(path, environ)
        self.assertEqual(loaded, ["DEEPSEEK_API_KEY"])
        self.assertEqual(environ["ANTHROPIC_API_KEY"], "from-process")
        self.assertEqual(environ["DEEPSEEK_API_KEY"], "from-file")

    def test_default_path_is_package_relative(self):
        self.assertEqual(config.env_file_path({}), config.PROJECT_DIR / ".env")
        self.assertEqual(config.PROJECT_DIR.name, "poc")
        self.assertEqual(config.PROJECT_DIR.parent.name, "spikes")
        self.assertEqual(config.env_file_path({"POC_ENV_FILE": "/x/.env"}), Path("/x/.env"))

    def test_missing_file_is_noop(self):
        self.assertEqual(config.load_env(Path("/nonexistent/.env"), {}), [])

    def test_redact(self):
        self.assertEqual(config.redact("key=sk-abc123 end", ["sk-abc123"]), "key=*** end")


class RegistryTest(unittest.TestCase):
    def test_env_example_lists_every_variable(self):
        example = config.parse_env_file(config.PROJECT_DIR / ".env.example")
        self.assertEqual(list(example), providers.all_env_vars())
        self.assertTrue(all(v == "" for v in example.values()), ".env.example 不能包含任何值")

    def test_registry_is_consistent(self):
        names = [p.name for p in providers.PROVIDERS]
        self.assertEqual(len(names), len(set(names)))
        for p in providers.PROVIDERS:
            self.assertTrue(p.env and p.domains and p.capabilities, p.name)
            self.assertTrue(set(p.capabilities) <= set(providers.CAPABILITIES), p.name)
            if p.probe:
                self.assertTrue(p.probe.url.startswith("https://"), p.name)
                self.assertIn(p.probe.url.split("/")[2], p.domains, p.name)
                self.assertTrue(p.probe.description.startswith("GET "), p.name)
