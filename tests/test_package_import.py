import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PackageImportTests(unittest.TestCase):
    def test_main_imports_as_an_astrbot_plugin_package(self):
        script = textwrap.dedent(
            f"""
            import importlib
            import sys
            import types

            class Decorators:
                class PermissionType:
                    ADMIN = object()
                class PlatformAdapterType:
                    KOOK = object()
                def __getattr__(self, name):
                    return lambda *args, **kwargs: (lambda func: func)

            astrbot = types.ModuleType("astrbot")
            api = types.ModuleType("astrbot.api")
            api.logger = types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, exception=lambda *a, **k: None)
            event = types.ModuleType("astrbot.api.event")
            event.AstrMessageEvent = type("AstrMessageEvent", (), {{}})
            event.filter = Decorators()
            star = types.ModuleType("astrbot.api.star")
            star.Context = type("Context", (), {{}})
            star.Star = type("Star", (), {{"__init__": lambda self, context: None}})
            star.register = lambda *args, **kwargs: (lambda cls: cls)
            sys.modules.update({{
                "astrbot": astrbot,
                "astrbot.api": api,
                "astrbot.api.event": event,
                "astrbot.api.star": star,
            }})

            try:
                import aiohttp
            except ModuleNotFoundError:
                aiohttp = types.ModuleType("aiohttp")
                aiohttp.ClientSession = type("ClientSession", (), {{}})
                aiohttp.ClientTimeout = type("ClientTimeout", (), {{}})
                aiohttp.ClientError = type("ClientError", (Exception,), {{}})
                aiohttp.ContentTypeError = type("ContentTypeError", (aiohttp.ClientError,), {{}})
                sys.modules["aiohttp"] = aiohttp

            package = types.ModuleType("astrbot_plugin_kook_name_beautify")
            package.__path__ = [{str(ROOT)!r}]
            sys.modules[package.__name__] = package
            module = importlib.import_module("astrbot_plugin_kook_name_beautify.main")
            assert module.__version__ == "0.2.3"
            fake_event = types.SimpleNamespace(message_str="lumi/kook美化确认 f5c251f1")
            assert module.KookNameBeautifyPlugin._has_explicit_plan_action(
                fake_event,
                "f5c251f1",
                ("/kook美化确认", "确认执行方案"),
            )
            assert not module.KookNameBeautifyPlugin._has_explicit_plan_action(
                types.SimpleNamespace(message_str="请执行吧"),
                "f5c251f1",
                ("/kook美化确认", "确认执行方案"),
            )
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
