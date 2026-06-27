"""通用 PPO 训练(godot_rl_agents + stable-baselines3),与具体游戏无关。
先跑本脚本(监听端口等 Godot 连入),再由 run_train.sh 启 Godot 连入。
所有路径/超参用环境变量配置。"""
import os
from godot_rl.wrappers.stable_baselines_wrapper import StableBaselinesGodotEnv
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import VecMonitor

TIMESTEPS = int(os.environ.get("TIMESTEPS", "60000"))
PORT = int(os.environ.get("PORT", "11008"))
SPEEDUP = int(os.environ.get("SPEEDUP", "8"))
MODEL_DIR = os.environ.get("MODEL_DIR",
                           os.path.expanduser("~/.local/share/godot-rl-venv"))
SAVE = os.environ.get("SAVE_PATH", f"{MODEL_DIR}/ppo_game.zip")
WARM = os.environ.get("WARM_START", "")   # 非空=从已有模型热启动继续训(保留已学技能)

print(f"=== PPO 训练:目标 {TIMESTEPS} 步,等待 Godot 连入 :{PORT} ===", flush=True)
env = StableBaselinesGodotEnv(env_path=None, port=PORT, show_window=False,
                              speedup=SPEEDUP, seed=1)
env = VecMonitor(env)   # → SB3 显示 ep_rew_mean / ep_len_mean,能看学习曲线
print("=== Godot 已连入,开始训练 ===", flush=True)

if WARM and os.path.exists(WARM):
    print(f"=== 热启动:加载 {WARM} 继续训练 ===", flush=True)
    model = PPO.load(WARM, env=env, device="cpu")
else:
    model = PPO(
        "MultiInputPolicy", env, verbose=1,
        n_steps=512, batch_size=512,
        gamma=0.99, gae_lambda=0.95, ent_coef=0.03,
        learning_rate=3e-4, device="cpu",
    )
model.learn(total_timesteps=TIMESTEPS)
model.save(SAVE)
env.close()
print(f"=== TRAIN_DONE saved {SAVE} ===", flush=True)
