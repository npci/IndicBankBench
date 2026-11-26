"""Contracts shipped with the harness; no downloaded cases required."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from indicbankbench.harness import judge, tools

class Disclosure(unittest.TestCase):

    def test_every_declared_prerequisite_is_disclosed(self):
        raw, _ = tools.load_tool_definitions()
        model = tools.load_model_schemas()
        undisclosed = []
        for tool in raw["tools"]:
            description = model[tool["name"]]["function"]["description"]
            for pre in tool.get("call_before") or []:
                if pre.get("tool") and pre["tool"] not in description:
                    undisclosed.append(f"{tool['name']} requires {pre['tool']}, unmentioned in its description")
        self.assertEqual([], undisclosed, "\n  " + "\n  ".join(undisclosed))
