"""通用推理回放:加载训练好的 PPO 策略(只 predict,不学习)。
Godot 非 headless 启动 → 开窗口渲染 + Recorder 截图。路径/参数走环境变量。

import-safe:模块顶层只做 import 与常量解析,绝不发起 TCP 连接;所有网络/环境
构造都在 main() 内进行,以便 tests 可纯逻辑导入 seed_everything / run_loop。
"""
import os
import random
import sys

import numpy as np
import torch


def _env_int(name, default):
    return int(os.environ.get(name, str(default)))


def seed_everything(seed):
    """固定 Python / NumPy / PyTorch 三处 RNG。模型与环境的 seed 另行注入。"""
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def run_loop(env, model, *, deterministic, eval_episodes, max_eval_steps):
    """按 episode 停止的推理主循环。

    累计 done(向量环境 done 是数组)中的完成局数,达到 eval_episodes 后再推进
    一次 reset 握手 step 让 Godot 侧完成复位,然后返回完成局数。
    若在达标前用尽 max_eval_steps,抛 RuntimeError(供调用方非 0 退出)。
    """
    obs = env.reset()
    completed = 0
    for step in range(max_eval_steps):
        action, _ = model.predict(obs, deterministic=deterministic)
        obs, _reward, done, _info = env.step(action)
        completed += int(np.count_nonzero(done))
        if completed >= eval_episodes:
            # 达标后再走一步,推进 godot_rl 的 reset 握手,避免遗留半个 episode。
            model.predict(obs, deterministic=deterministic)
            env.step(action)
            return completed
    raise RuntimeError(
        f"达到 {completed}/{eval_episodes} 局即用尽 MAX_EVAL_STEPS={max_eval_steps};"
        f"评估失败"
    )


def main():
    from godot_rl.wrappers.stable_baselines_wrapper import StableBaselinesGodotEnv
    from stable_baselines3 import PPO

    port = _env_int("PORT", 11008)
    speedup = _env_int("SPEEDUP", 8)  # 必须与训练一致!
    seed = _env_int("EVAL_SEED", 3)
    eval_episodes = _env_int("EVAL_EPISODES", 5)
    max_eval_steps = _env_int("MAX_EVAL_STEPS", 40000)
    model_dir = os.environ.get(
        "MODEL_DIR", os.path.expanduser("~/.local/share/godot-rl-venv"))
    model_path = os.environ.get("MODEL", f"{model_dir}/ppo_game.zip")
    # 若某技能是「概率性」学会的(如缺口边以 ~40% 概率起跳),argmax 会把它压成不触发→每次失败,
    # 此时用随机采样 deterministic=False 才能复现训练时的行为。默认 False。
    deterministic = os.environ.get("DETERMINISTIC", "0") == "1"

    # 固定种子链:Python/NumPy/PyTorch → 环境 → 模型,与 Godot --env_seed 同源。
    seed_everything(seed)

    print(f"=== 推理:等待 Godot 连入 :{port}"
          f"(deterministic={deterministic}, seed={seed}) ===", flush=True)
    env = StableBaselinesGodotEnv(env_path=None, port=port, show_window=True,
                                  speedup=speedup, seed=seed)
    model = PPO.load(model_path, device="cpu")
    model.set_random_seed(seed)
    print(f"=== 模型已加载({model_path}),开始回放"
          f"(目标 {eval_episodes} 局,上限 {max_eval_steps} 步) ===", flush=True)

    try:
        completed = run_loop(env, model, deterministic=deterministic,
                             eval_episodes=eval_episodes,
                             max_eval_steps=max_eval_steps)
    except RuntimeError as exc:
        env.close()
        print(f"=== 推理失败:{exc} ===", flush=True)
        sys.exit(1)

    env.close()
    print(f"=== 推理结束(完成 {completed} 局) ===", flush=True)


if __name__ == "__main__":
    main()
