"""infer_rl 的纯逻辑测试:固定种子链 + 按 episode 停止 + 步数上限。

不真连 Godot:env/model 用桩替身,seed 函数用 monkeypatch 拦截。
infer_rl.py 必须 import-safe(顶层 import 不发起任何网络/TCP 连接)。
"""
import numpy as np
import pytest

import infer_rl


def test_seed_everything_sets_python_numpy_torch(monkeypatch):
    """seed_everything 必须把同一个 seed 喂给 random / numpy / torch。"""
    seen = {}
    monkeypatch.setattr(infer_rl.random, "seed", lambda s: seen.__setitem__("py", s))
    monkeypatch.setattr(infer_rl.np.random, "seed", lambda s: seen.__setitem__("np", s))
    monkeypatch.setattr(infer_rl.torch, "manual_seed", lambda s: seen.__setitem__("torch", s))

    infer_rl.seed_everything(1234)

    assert seen == {"py": 1234, "np": 1234, "torch": 1234}


class _FakeEnv:
    """向量化 env 桩:step 按预设 done 序列返回,记录 reset/step 次数。"""

    def __init__(self, done_sequence):
        self._done_seq = list(done_sequence)
        self._i = 0
        self.reset_calls = 0
        self.step_calls = 0

    def reset(self):
        self.reset_calls += 1
        return np.zeros((1, 1), dtype=np.float32)

    def step(self, action):
        done = self._done_seq[self._i] if self._i < len(self._done_seq) else np.array([False])
        self._i += 1
        self.step_calls += 1
        return np.zeros((1, 1), dtype=np.float32), np.zeros(1), np.asarray(done), [{}]


class _FakeModel:
    def predict(self, obs, deterministic=False):
        return np.zeros(1, dtype=np.int64), None


def test_should_stop_only_after_episode_target():
    """3 个 done 出现在不同步,累计满 EVAL_EPISODES=3 才停;返回完成局数。"""
    # done 在第 2、4、5 步出现,其余为 False
    seq = [np.array([False]), np.array([True]), np.array([False]),
           np.array([True]), np.array([True]), np.array([False])]
    env = _FakeEnv(seq)
    model = _FakeModel()

    completed = infer_rl.run_loop(env, model, deterministic=False,
                                  eval_episodes=3, max_eval_steps=100)

    assert completed == 3
    # 第 5 步(index 4)第 3 局完成,之后再推一次 reset 握手 step
    assert env.step_calls == 6


def test_step_budget_fails_before_episode_target():
    """预算 max_eval_steps 先于 episode 目标耗尽 → RuntimeError。"""
    seq = [np.array([False])] * 10  # 永远不结束
    env = _FakeEnv(seq)
    model = _FakeModel()

    with pytest.raises(RuntimeError):
        infer_rl.run_loop(env, model, deterministic=False,
                          eval_episodes=3, max_eval_steps=5)
