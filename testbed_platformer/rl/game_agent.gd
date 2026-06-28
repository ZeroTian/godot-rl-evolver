extends AIController2D
## 真游戏 RL 控制器（虚拟手柄）：
##   - 动作 → Input.action_press/release，让真主角的 FSM/动画/攻击判定原样运行（不改真游戏脚本）。
##   - 观测真主角状态 + 前方缺口 + 最近敌人 + 血量。
##   - 奖励 = 向右推进 + 杀敌 - 受伤 - 坠落，到终点大奖。
## episode 的复位/敌人重生交给 game_env.gd（env）。

const SPEED_REF := 300.0
const GOAL_X := 1520.0
const FALL_Y := 120.0     # 主角脚下地面 y≈0，掉到 y>120 视为坠落
const HP_FAIL := 40       # 血量降到此值即判负（单次伤害 10，永远到不了 0，规避主角自杀 queue_free）
const MAX_EP := 1500      # 每局最大物理帧

var env: Node = null      # game_env.gd 根节点
var tele = null            # telemetry helper(由 env 注入)
var _last_term := "unknown"
var _last_end_pos := Vector2.ZERO
var _pending_record := false   # 仅在真实终止条件触发时记录本局(过滤 godot_rl reset 产生的伪局)

var _prev_jump := false
var _prev_attack := false
var _prev_x := 0.0
var _prev_hp := 100
var _prev_mc := 0
var _prev_mhp := 0           # 上一帧敌人总血量(用于「造成伤害」密集奖励)
var ep := 0
var _atk_presses := 0        # 统计:本次运行按了多少次攻击(验证战斗是否被激活)
var _atk_edge := false       # 本帧是否「刚发起」一次攻击(用于"靠近敌人挥砍"塑形)
var _crossed_gap := false    # 本局是否已跨过缺口(只奖一次)
var _was_on_floor := true    # 上一帧是否在地面(检测起跳边沿)
var dbg: FileAccess = null   # 临时诊断
var _w: Dictionary = {}      # persona reward 权重覆盖(仅 PERSONA env 非空时填充;空 → 全用字面默认)


func _ready() -> void:
	super._ready()        # add_to_group("AGENT")
	reset_after = 1000000 # 关掉插件自带超时，自己管 episode 长度
	dbg = FileAccess.open("res://rl/game_dbg.log", FileAccess.WRITE)
	# 训练期 persona 权重加载：仅当 PERSONA env 非空时读取覆盖值；推理/panel 路径不读。
	var persona_name := OS.get_environment("PERSONA")
	if persona_name != "":
		var path := "res://../personas/" + persona_name + ".json"
		var f := FileAccess.open(path, FileAccess.READ)
		if f != null:
			var txt := f.get_as_text()
			f.close()
			var parsed = JSON.parse_string(txt)
			if parsed is Dictionary and parsed.has("reward_weights"):
				var rw = parsed["reward_weights"]
				if rw is Dictionary:
					_w = rw
		# 读失败/字段缺 → _w 保持空，reward 退回字面默认，不崩。


## persona 权重取值辅助：_w 中有键则取覆盖值，否则用字面默认(训练期无 PERSONA 时 _w 为空)。
func _wget(key: String, lit: float) -> float:
	return _w.get(key, lit)


func _d(s: String) -> void:
	if dbg:
		dbg.store_line(s)
		dbg.flush()


## 由 env 在 _ready 调用：绑定主角 + 环境，并初始化各项跟踪量。
func bind(player_body: Node2D, env_node: Node) -> void:
	init(player_body)     # 设置 _player
	env = env_node
	_init_trackers()


func _init_trackers() -> void:
	ep = 0
	_prev_x = _player.global_position.x
	_prev_hp = env.player_hp()
	_prev_mc = env.monster_count()
	_prev_mhp = env.total_monster_hp()
	_prev_jump = false
	_prev_attack = false
	_crossed_gap = false
	_was_on_floor = true


# ---------- godot_rl 接口 ----------

func get_obs() -> Dictionary:
	var p := _player
	var nm: Node2D = env.nearest_monster(p.global_position)
	var mdx := 1.0
	var mdy := 0.0
	var alive := 0.0
	if nm != null:
		mdx = clampf((nm.global_position.x - p.global_position.x) / 600.0, -1.0, 1.0)
		mdy = clampf((nm.global_position.y - p.global_position.y) / 300.0, -1.0, 1.0)
		alive = 1.0
	var obs := [
		clampf(p.velocity.x / SPEED_REF, -1.0, 1.0),
		clampf(p.velocity.y / 500.0, -1.0, 1.0),
		1.0 if p.is_on_floor() else 0.0,
		clampf((GOAL_X - p.global_position.x) / 1600.0, -1.0, 1.0),  # 距终点
		clampf(p.global_position.x / 1600.0, 0.0, 1.0),             # 进度
		env.gap_ahead(p.global_position),                          # 前方有无地面(0=缺口)
		mdx, mdy, alive,                                           # 最近敌人相对位置+存活
		clampf(float(env.player_hp()) / 100.0, 0.0, 1.0),          # 血量
	]
	return {"obs": obs}


func get_action_space() -> Dictionary:
	return {
		"move": {"size": 3, "action_type": "discrete"},   # 0 左 / 1 停 / 2 右
		"jump": {"size": 2, "action_type": "discrete"},    # 0 否 / 1 跳
		"attack": {"size": 2, "action_type": "discrete"},  # 0 否 / 1 攻击
	}


func set_action(action) -> void:
	var mv := _disc(action["move"])
	Input.action_release("move_left")
	Input.action_release("move_right")
	if mv == 0:
		Input.action_press("move_left")
	elif mv == 2:
		Input.action_press("move_right")

	# 跳/攻击是 just_pressed 触发：只在动作 0→1 的那一帧按下，松开后才能再次触发
	var jp := _disc(action["jump"]) == 1
	if jp and not _prev_jump:
		Input.action_press("jump")
	elif not jp:
		Input.action_release("jump")
	_prev_jump = jp

	var at := _disc(action["attack"]) == 1
	if at and not _prev_attack:
		Input.action_press("attack")
		_atk_presses += 1
		_atk_edge = true
	elif not at:
		Input.action_release("attack")
	_prev_attack = at

	if tele:
		tele.record_action(action)


func get_reward() -> float:
	return reward


func _disc(a) -> int:
	if a is Array:
		return int(a[0])
	return int(a)


# ---------- 每帧推进：奖励累计 + done 判定 + 复位 ----------

func _physics_process(_delta: float) -> void:
	if needs_reset:
		_d("EP_END 累计攻击按下=%d" % _atk_presses)
		if tele and _pending_record:
			tele.end_episode({"term": _last_term, "end_pos": [_last_end_pos.x, _last_end_pos.y], "max_x": _last_end_pos.x})
		_pending_record = false
		# done 保活:不在此清零,交给 Sync 控制步 _get_done_from_agents 读取后 set_done_false。
		# 否则 done 只存活 1 物理帧,被 action_repeat 门控的 Sync 多半采样不到 → Python 收到的
		# done 计数与真实局数脱钩(~20x)。本版 godot_rl 的 _reset_agents_if_done 已注释、基类
		# reset() 不动 done、telemetry 仅在 _pending_record 记录,故保活不会复活伪局。
		env.reset_episode()
		reset()            # 基类：n_steps=0, needs_reset=false
		_init_trackers()
		return

	ep += 1
	var _r_before := reward
	var p := _player

	# 推进奖励：向右接近终点为正
	reward += (p.global_position.x - _prev_x) * _wget("progress", 0.01)
	reward -= _wget("time_penalty", 0.002)   # 时间惩罚，催促前进
	_prev_x = p.global_position.x

	# 战斗奖励:① 密集「造成伤害」(打掉敌人血就给分,教会"攻击划算")
	var mhp: int = env.total_monster_hp()
	if mhp < _prev_mhp:
		reward += float(_prev_mhp - mhp) * _wget("damage", 0.1)   # 每点伤害 +0.1 → 一击(-20)= +2
	_prev_mhp = mhp
	# ② 击杀大奖(+25：顺路杀掉火骑士严格优于无视它,把战斗诱导出来)
	var mc: int = env.monster_count()
	if mc < _prev_mc:
		reward += _wget("kill", 25.0) * float(_prev_mc - mc)
		_d("KILL ep=%d x=%.0f 剩余敌人=%d" % [ep, p.global_position.x, mc])
	_prev_mc = mc
	# ③ 战斗塑形:在敌人身旁(≤40px)发起攻击 → +0.5,诱导"靠近就挥砍"(连上后伤害/击杀奖励接管)
	if _atk_edge:
		_atk_edge = false
		var nm: Node2D = env.nearest_monster(p.global_position)
		if nm != null and absf(nm.global_position.x - p.global_position.x) <= 40.0:
			reward += _wget("combat_shape", 0.5)

	# 受伤惩罚
	var hp: int = env.player_hp()
	if hp < _prev_hp:
		reward -= _wget("hurt_penalty", 0.5) * float(_prev_hp - hp) / 10.0
	_prev_hp = hp

	# 缺口子技能塑形：分两层密集信号教"看到缺口→在边缘起跳→落到对岸"
	var on_floor: bool = p.is_on_floor()
	var near_gap: bool = env.gap_ahead(p.global_position) < 0.5   # 前方一格是缺口
	# (a) 在缺口边缘起跳(脚刚离地且前方是缺口)→ 立即 +1，教起跳时机
	if _was_on_floor and not on_floor and near_gap:
		reward += _wget("gap_edge_jump", 1.0)
	# (b) 成功落到对岸(x≥630 且站稳)→ 一次性 +8
	if not _crossed_gap and p.global_position.x >= 630.0 and on_floor:
		reward += _wget("gap_cross", 8.0)
		_crossed_gap = true
		_d("CROSS GAP ep=%d x=%.0f" % [ep, p.global_position.x])
	_was_on_floor = on_floor

	# 终止判定（终点不门控：保留 +30 让"跨缺口"技能稳住，靠 +25 击杀奖励诱导战斗）
	if p.global_position.x >= GOAL_X:
		reward += _wget("goal", 30.0)
		done = true
		_d("DONE GOAL ep=%d x=%.0f rew=%.2f" % [ep, p.global_position.x, reward])
	elif p.global_position.y > FALL_Y:
		reward -= _wget("fall", 10.0)
		done = true
		_d("DONE FALL ep=%d x=%.0f y=%.0f rew=%.2f" % [ep, p.global_position.x, p.global_position.y, reward])
	elif hp <= HP_FAIL:
		reward -= _wget("hp_fail", 10.0)
		done = true
		_d("DONE HP ep=%d x=%.0f hp=%d rew=%.2f" % [ep, p.global_position.x, hp, reward])
	elif ep >= MAX_EP:
		done = true
		_d("DONE TIMEOUT ep=%d x=%.0f rew=%.2f" % [ep, p.global_position.x, reward])

	# godot_rl 不会因 done 自动复位：终止时自己置 needs_reset，下一帧走复位路径
	# telemetry: 每帧采集本帧回报增量与位置
	if tele:
		tele.tick(reward - _r_before, p.global_position)
	if done:
		var _real := true
		if p.global_position.x >= GOAL_X:
			_last_term = "goal"
		elif p.global_position.y > FALL_Y:
			_last_term = "fall"
		elif hp <= HP_FAIL:
			_last_term = "hp"
		elif ep >= MAX_EP:
			_last_term = "timeout"
		else:
			_real = false   # done 为真但无真实终止条件 → godot_rl 时序产生的伪 done,不记
		if _real:
			_last_end_pos = p.global_position
			_pending_record = true
			if tele and (_last_term == "fall" or _last_term == "hp"):
				tele.emit_event("death", {"pos": [p.global_position.x, p.global_position.y], "cause": _last_term})
		needs_reset = true
