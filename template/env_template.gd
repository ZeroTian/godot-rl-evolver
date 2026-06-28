extends Node2D
## ── env 根骨架 ───────────────────────────────────────────────────────
## 职责:把「真地图 + 真角色 + 真敌人 + Sync 节点 + Agent」拼在一起;
##       负责 episode 复位(角色归位 + 敌人/道具重生 + 清场)和给 Agent 提供查询。
## 不改任何游戏脚本——全靠在外部读写节点状态。
##
## 训练场景里挂法(.tscn):
##   根(本脚本) ├─ Sync(addons/godot_rl_agents/sync.gd, speed_up=8)
##              ├─ (真地图/角色/敌人...)
##              └─ Agent(agent_template.gd 的子类)

# ★ 度量:把 harness/telemetry.gd 拷到 res://rl/ 后预载
const Telemetry = preload("res://rl/telemetry.gd")

@onready var player: CharacterBody2D = $Player
@onready var agent: AIController2D = $Agent

var tele = null              # ★ 度量:采集 helper,_ready 创建并注入给 agent


func _ready() -> void:
	agent.bind(player, self)
	# ★ 度量:开一次 run(落盘 res://rl/telemetry/run_<ts>.jsonl),并注入 agent
	#   action_space 用于动作分布维度;max_ep 供诊断器判断「卡住/超时」
	tele = Telemetry.new()
	tele.start_run({
		"scene": get_scene_file_path(),
		"model": OS.get_environment("MODEL"),
		"speedup": 8, "grid_cell": 64, "max_ep": 1500,
		"action_space": {"move": 3, "jump": 2, "attack": 2},
	})
	agent.tele = tele
	_reset_to_start()


func reset_episode() -> void:
	_reset_to_start()


func _exit_tree() -> void:
	# ★ 度量:推理/训练结束时 flush + 关闭 JSONL
	if tele:
		tele.finish()


func _reset_to_start() -> void:
	# 松开所有虚拟按键
	for a in ["move_left", "move_right", "jump", "attack"]:
		Input.action_release(a)
	# ★ FILL ★ 角色归位:position / velocity / health / 状态机回 idle
	#   注意:若你的角色「死亡时 queue_free 自己」,RL 常驻环境会被破坏 →
	#   要么给训练用角色超高血量永不死,要么在血量到阈值前就判负复位。
	# ★ FILL ★ 敌人/道具:queue_free 旧的 + 重新实例化到起始位(注意 queue_free 是延迟的,
	#   查询时用 is_queued_for_deletion() 过滤,避免把正在释放的算进去)


# ── 给 Agent 的查询(按你的观测/奖励所需扩展)──────────────────────────
func player_hp() -> int:
	return player.health
