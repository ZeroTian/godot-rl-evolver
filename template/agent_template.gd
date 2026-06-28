extends AIController2D
## ── RL 控制器骨架(虚拟手柄)──────────────────────────────────────────
## 复用点:用 Input.action_press/release 驱动「现成游戏」的 FSM,不改游戏脚本。
## 接新游戏只需填 4 个钩子(下方 ★ FILL ★),其余(reset/done 握手、指标、telemetry)通用。
##
## 挂在被控角色下(或 env 根),由 env 根在 _ready 里 init(player) + 绑定 env + 注入 tele。
## 配套:同目录 env_template.gd(env 根) + telemetry.gd(度量采集) + 一个挂了 Sync 节点的训练场景。

const SPEED_REF := 300.0     # 观测归一化用的速度基准(按你的游戏调)
const MAX_EP := 1500         # 每局最大物理帧(超时结束)

var env: Node = null         # env 根(env_template.gd)
var ep := 0
var _prev_jump := false
var _prev_attack := false
# —— 通用指标(自动试玩用):攻击次数等,按需扩展 ——
var _atk_presses := 0
var dbg: FileAccess = null

# —— ★ 度量(telemetry)——由 env 在 _ready 注入(agent.tele = tele);未注入时全部 no-op ——
var tele = null
var _last_term := "unknown"          # 本局终止原因(由 done 分支按真实条件设置)
var _last_end_pos := Vector2.ZERO    # 本局终止位置(reset 前记录,死亡热点用)
var _pending_record := false         # 仅真实终止才记录,过滤 godot_rl reset 产生的伪局


func _ready() -> void:
	super._ready()           # add_to_group("AGENT")
	reset_after = 1000000    # 关掉插件自带超时,自己用 MAX_EP 管 episode 长度
	dbg = FileAccess.open("res://rl/game_dbg.log", FileAccess.WRITE)


func bind(player_body: Node2D, env_node: Node) -> void:
	init(player_body)        # 设置 _player
	env = env_node
	_init_trackers()


func _init_trackers() -> void:
	ep = 0
	_prev_jump = false
	_prev_attack = false
	# ★ FILL ★ 这里重置你在奖励里用到的「上一帧量」(如 _prev_x、_prev_hp ...)


func _d(s: String) -> void:
	if dbg: dbg.store_line(s); dbg.flush()


# ── ★ FILL ★ 钩子 1:观测(归一化到 ~[-1,1],网络好学)────────────────
func get_obs() -> Dictionary:
	var p := _player
	var obs := [
		clampf(p.velocity.x / SPEED_REF, -1.0, 1.0),
		# ... 填你的观测:位置/距目标/前方地形/最近敌人/血量 ...
	]
	return {"obs": obs}


# ── ★ FILL ★ 钩子 2:动作空间(建议「全离散」,避免 SB3 不支持的连续+离散混合)──
func get_action_space() -> Dictionary:
	return {
		"move": {"size": 3, "action_type": "discrete"},    # 0左 1停 2右
		"jump": {"size": 2, "action_type": "discrete"},
		"attack": {"size": 2, "action_type": "discrete"},
	}


# ── ★ FILL ★ 钩子 3:动作→虚拟手柄(注入 Input,驱动真游戏 FSM)──────────
func set_action(action) -> void:
	var mv := _disc(action["move"])
	Input.action_release("move_left"); Input.action_release("move_right")
	if mv == 0: Input.action_press("move_left")
	elif mv == 2: Input.action_press("move_right")
	# jump/attack 多为 is_action_just_pressed → 只在 0→1 边沿按一帧,松开才能再触发
	var jp := _disc(action["jump"]) == 1
	if jp and not _prev_jump: Input.action_press("jump")
	elif not jp: Input.action_release("jump")
	_prev_jump = jp
	var at := _disc(action["attack"]) == 1
	if at and not _prev_attack: Input.action_press("attack"); _atk_presses += 1
	elif not at: Input.action_release("attack")
	_prev_attack = at

	# ★ 度量:记录动作分布(各档使用率 + 动作序列熵 → 冗余/单调诊断)
	if tele:
		tele.record_action(action)


func get_reward() -> float:
	return reward            # reward 在 _physics_process 累计,sync 每步读后清零


func _disc(a) -> int:
	return int(a[0]) if a is Array else int(a)


# ── 每帧推进:奖励累计 + done 判定 + 复位(握手通用,奖励/终止 ★FILL★)──────
func _physics_process(_delta: float) -> void:
	# 复位握手:godot_rl 不会因 done 自动复位 → 终止时本类自己置了 needs_reset
	if needs_reset:
		_d("EP_END atk=%d" % _atk_presses)
		# ★ 度量:在 env.reset_episode() 之前 end_episode(reset 会把角色归位,终止位置会丢)
		#   只记真实终止(_pending_record),过滤 godot_rl reset 时序产生的伪局
		if tele and _pending_record:
			tele.end_episode({"term": _last_term,
				"end_pos": [_last_end_pos.x, _last_end_pos.y]})
		_pending_record = false
		# ★ done 保活:不在此清零,交给 Sync 控制步 _get_done_from_agents 读后 set_done_false。
		#   原因:done 只活 1 物理帧时,被 action_repeat(默认 8)门控的 Sync 多半采样不到 →
		#   Python 收到的 done 计数与真实局数严重脱钩(实测 ~20x),EVAL_EPISODES 失效。
		#   适用前提(godot_rl_agents 默认即满足):_reset_agents_if_done 未启用(训练路径已注释)、
		#   基类 reset() 不动 done、telemetry 仅在 _pending_record 记录(故保活不复活伪局)。
		#   若你的 godot_rl 版本启用了 _reset_agents_if_done,改回 `done = false` 更稳(但 EVAL_EPISODES 会不精确)。
		env.reset_episode()
		reset()              # 基类:n_steps=0, needs_reset=false
		_init_trackers()
		return

	ep += 1
	var p := _player
	var _r_before := reward   # ★ 度量:帧初基线,帧末算本帧回报增量

	# ★ FILL ★ 钩子 4a:稠密奖励(向目标推进 + 子技能塑形)
	#   经验:稀疏大奖学不会硬探索动作 → 必须配「行为塑形」(如缺口边起跳+1、近敌挥砍+0.5)
	# reward += ...

	# ★ 度量:每帧采集本帧回报增量 + 位置(局长/回报/探索覆盖)
	if tele:
		tele.tick(reward - _r_before, p.global_position)

	# ★ FILL ★ 钩子 4b:终止判定(到目标 +大奖 done;失败 -惩罚 done;超时 done)
	#   经验:别用「全或无」门控锚定奖励,会摧毁前置技能。
	if false: # reached_goal:
		reward += 30.0; done = true
	elif ep >= MAX_EP:
		done = true

	# 关键:终止时必须自己置 needs_reset,下一帧才会走复位(否则 agent 死了不重生)
	if done:
		# ★ 度量:按真实终止条件设 term + 死亡事件;_real=false 表示 godot_rl 伪 done,不记
		var _real := true
		# ★ FILL ★ 按你的游戏终止条件分类(与上面钩子 4b 对应):
		#   if reached_goal: _last_term = "goal"
		#   elif fell:       _last_term = "fall"
		#   elif hp_dead:    _last_term = "hp"
		#   elif ep >= MAX_EP: _last_term = "timeout"
		#   else: _real = false
		if ep >= MAX_EP:
			_last_term = "timeout"
		else:
			_real = false
		if _real:
			_last_end_pos = p.global_position
			_pending_record = true
			# 失败类终止 emit 死亡事件(死亡热点诊断):
			#   if tele and (_last_term == "fall" or _last_term == "hp"):
			#       tele.emit_event("death", {"pos": [p.global_position.x, p.global_position.y], "cause": _last_term})
		needs_reset = true
