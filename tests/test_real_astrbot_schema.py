import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class RealAstrBotSchemaTests(unittest.TestCase):
    def test_guard_tools_register_with_real_astrbot(self):
        parent = Path(__file__).resolve().parents[2]
        app = Path(os.environ.get("ASTRBOT_APP_PATH", str(Path(sys.executable).resolve().parent.parent / "app")))
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
        paths = [str(parent)]
        if (app / "astrbot").is_dir():
            paths.insert(0, str(app))
        script = '''
import inspect
import json
import sys
sys.path[:0] = PATHS
import astrbot_plugin_group_invite_guard.main as module
from astrbot.core.provider.register import llm_tools
names = {
    "group_invite_ban_user": ("llm_ban_user", "user_id"),
    "group_invite_unban_user": ("llm_unban_user", "user_id"),
    "group_invite_query_ban": ("llm_query_ban", ""),
    "group_invite_query_profile": ("llm_query_profile", "qq"),
}
payload = {}
for name, (method, parameter) in names.items():
    tool = llm_tools.get_func(name)
    payload[name] = {"schema": tool.parameters}
    if parameter:
        item = inspect.signature(getattr(module.GroupInviteGuardPlugin, method)).parameters[parameter]
        payload[name]["required"] = item.default is inspect.Parameter.empty
print(json.dumps(payload))
'''.replace("PATHS", repr(paths))
        with tempfile.TemporaryDirectory() as workdir:
            result = subprocess.run([sys.executable, "-c", script], cwd=workdir, env=env,
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        for name, parameter in (("group_invite_ban_user", "user_id"),
                                ("group_invite_unban_user", "user_id"),
                                ("group_invite_query_profile", "qq")):
            self.assertEqual(payload[name]["schema"]["properties"][parameter]["type"], "string")
            self.assertTrue(payload[name]["required"])
        self.assertIn("group_invite_query_ban", payload)
