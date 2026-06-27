"""通用推理回放:加载训练好的 PPO 策略(只 predict,不学习)。
Godot 非 headless 启动 → 开窗口渲染 + Recorder 截图。路径/参数走环境变量。"""
import os
from godot_rl.wrappers.stable_baselines_wrapper import StableBaselinesGodotEnv
from stable_baselines3 import PPO

PORT = int(os.environ.get("PORT", "11008"))
SPEEDUP = int(os.environ.get("SPEEDUP", "8"))   # 必须与训练一致!
STEPS = int(os.environ.get("INFER_STEPS", "600"))
MODEL_DIR = os.environ.get("MODEL_DIR",
                           os.path.expanduser("~/.local/share/godot-rl-venv"))
MODEL = os.environ.get("MODEL", f"{MODEL_DIR}/ppo_game.zip")
# 若某技能是「概率性」学会的(如缺口边以 ~40% 概率起跳),argmax 会把它压成不触发→每次失败,
# 此时用随机采样 deterministic=False 才能复现训练时的行为。默认 False。
DET = os.environ.get("DETERMINISTIC", "0") == "1"

print(f"=== 推理:等待 Godot 连入 :{PORT}(deterministic={DET}) ===", flush=True)
env = StableBaselinesGodotEnv(env_path=None, port=PORT, show_window=True,
                              speedup=SPEEDUP, seed=3)
model = PPO.load(MODEL, device="cpu")
print(f"=== 模型已加载({MODEL}),开始回放 ===", flush=True)

obs = env.reset()
for i in range(STEPS):
    action, _ = model.predict(obs, deterministic=DET)
    obs, reward, done, info = env.step(action)
env.close()
print("=== 推理结束 ===", flush=True)
