"""让测试能 import harness/diagnose.py(harness 非包,加入 sys.path)。"""
import os
import sys

_HARNESS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "harness")
if _HARNESS not in sys.path:
    sys.path.insert(0, _HARNESS)
