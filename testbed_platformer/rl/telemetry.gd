extends RefCounted
## 通用 telemetry 采集 helper(虚拟手柄式 RL 试玩的"度量"环)。
## 累计通用指标(局长/回报/动作占比/动作熵/探索覆盖/终止位置),按 episode 落盘 JSONL。
## 落盘格式正是 harness/diagnose.py 消费的契约。详见
## docs/superpowers/specs/2026-06-28-telemetry-diagnosis-design.md §4.1 / §5.1
##
## 用法(在 env / agent 里):
##   var tele = preload("res://rl/telemetry.gd").new()
##   tele.start_run({"scene": ..., "model": ..., "speedup": 8, "n_episodes": 50,
##                   "action_space": {"move": 3, "jump": 2, "attack": 2}})
##   # set_action() 内:        tele.record_action(action)
##   # _physics_process() 每帧: tele.tick(本帧reward增量, player.global_position)
##   # 终止/语义(可选):        tele.emit_event("death", {"pos": [..], "cause": "fall"})
##   #                          tele.set_metric("hp_left", hp)
##   # reset 握手时:            tele.end_episode({"term": "fall", "end_pos": [..]})
##   # 推理结束:                tele.finish()
##
## 容错:未 start_run(_f == null)时所有采集方法静默 no-op,绝不让游戏崩。

var _f: FileAccess = null
var _run_id := ""
var _grid_cell := 64
var _ep := 0
var _action_space := {}          # {dim: size}(若 start_run 提供则预分配计数数组)

# —— 局内累计(每 end_episode 后重置)——
var _len := 0
var _return := 0.0
var _action_steps := 0           # record_action 调用次数(= 动作步数)
var _action_counts := {}         # {dim: Array[int]}(各档位计数)
var _action_seq_counts := {}     # {combo_key: int}(动作组合频次 → 动作序列熵)
var _visit := {}                 # {Vector2i: int}(网格访问计数 → 覆盖 cells/entropy)
var _events := []                # 本局语义事件(内嵌写入 episode 行的 events 字段)
var _metrics := {}               # set_metric 写入
var _last_pos := Vector2.ZERO    # 最近一帧位置(end_pos 兜底)


## 打开 JSONL 并写 run 头。cfg 可含:dir/scene/model/speedup/n_episodes/
## action_space({dim:size})/grid_cell。目录优先 cfg.dir,其次环境变量
## TELEMETRY_DIR,再次 "res://rl/telemetry"。
func start_run(cfg: Dictionary = {}) -> void:
	_grid_cell = int(cfg.get("grid_cell", 64))
	_action_space = cfg.get("action_space", {})
	_run_id = str(int(Time.get_unix_time_from_system()))

	var dir: String = cfg.get("dir", "")
	if dir == "":
		dir = OS.get_environment("TELEMETRY_DIR")
	if dir == "":
		dir = "res://rl/telemetry"
	DirAccess.make_dir_recursive_absolute(ProjectSettings.globalize_path(dir))

	var path := dir.path_join("run_%s.jsonl" % _run_id)
	_f = FileAccess.open(path, FileAccess.WRITE)
	if _f == null:
		push_warning("[telemetry] 无法打开 %s,采集关闭" % path)
		return

	_reset_episode_state()
	var header := {
		"type": "run", "run_id": _run_id,
		"scene": cfg.get("scene", ""), "model": cfg.get("model", ""),
		"speedup": cfg.get("speedup", 0),
		"n_episodes": cfg.get("n_episodes", 0),
		"action_space": _action_space,
		"grid": {"cell": _grid_cell},
	}
	if cfg.has("max_ep"):
		header["max_ep"] = cfg["max_ep"]
	if cfg.has("ver"):
		header["ver"] = cfg["ver"]
	_write(header)


## 在 agent.set_action() 内调用:累计各动作维度各档位使用次数 + 动作组合频次。
func record_action(action: Dictionary) -> void:
	if _f == null:
		return
	_action_steps += 1
	var combo := []
	# 按维度名排序保证组合 key 稳定
	var dims := action.keys()
	dims.sort()
	for dim in dims:
		var idx := _disc(action[dim])
		var arr: Array = _action_counts.get(dim, [])
		# 按需扩展(size 未知或档位超出预分配长度时)
		var need: int = maxi(idx + 1, int(_action_space.get(dim, 0)))
		while arr.size() < need:
			arr.append(0)
		arr[idx] += 1
		_action_counts[dim] = arr
		combo.append(idx)
	var key := str(combo)
	_action_seq_counts[key] = int(_action_seq_counts.get(key, 0)) + 1


## 在 agent._physics_process() 每帧调用:累计步数/本帧回报增量/网格访问。
func tick(reward_delta: float, pos: Vector2) -> void:
	if _f == null:
		return
	_len += 1
	_return += reward_delta
	_last_pos = pos
	var cell := Vector2i(floori(pos.x / _grid_cell), floori(pos.y / _grid_cell))
	_visit[cell] = int(_visit.get(cell, 0)) + 1


## 可选语义事件(如 death/checkpoint/kill)。内嵌进本局 episode 行的 events 字段。
func emit_event(name: String, data: Dictionary = {}) -> void:
	if _f == null:
		return
	var ev := {"name": name, "frame": _len}
	for k in data:
		ev[k] = data[k]
	_events.append(ev)


## 可选自定义标量,写进本局 episode 行的 metrics 字段。
func set_metric(key: String, value) -> void:
	if _f == null:
		return
	_metrics[key] = value


## episode 结束(reset 握手处)调用:算占比/熵/覆盖/终止位置,写一行 episode,重置局内累计。
func end_episode(info: Dictionary = {}) -> void:
	if _f == null:
		return
	var actions := {}
	for dim in _action_counts:
		var arr: Array = _action_counts[dim]
		var props := []
		for c in arr:
			props.append((float(c) / _action_steps) if _action_steps > 0 else 0.0)
		actions[dim] = props

	var end_pos: Array = info.get("end_pos", [_last_pos.x, _last_pos.y])
	var rec := {
		"type": "episode", "run_id": _run_id, "ep": _ep,
		"len": _len, "return": _return,
		"term": info.get("term", "unknown"),
		"actions": actions,
		"action_entropy": _entropy(_action_seq_counts.values()),
		"coverage": {"cells": _visit.size(), "entropy": _entropy(_visit.values())},
		"end_pos": end_pos,
		"events": _events.duplicate(true),
		"metrics": _metrics.duplicate(true),
	}
	_write(rec)
	_ep += 1
	_reset_episode_state()


## flush + 关闭文件。推理结束时调用。
func finish() -> void:
	if _f == null:
		return
	_f.flush()
	_f.close()
	_f = null


# ── 内部 ──────────────────────────────────────────────────────────────

func _reset_episode_state() -> void:
	_len = 0
	_return = 0.0
	_action_steps = 0
	_action_counts = {}
	_action_seq_counts = {}
	_visit = {}
	_events = []
	_metrics = {}
	_last_pos = Vector2.ZERO


## 离散动作取档位(兼容 Array / int,与 agent 的 _disc 同义)。
func _disc(a) -> int:
	if a is Array:
		return int(a[0]) if a.size() > 0 else 0
	return int(a)


## Shannon 熵(以 2 为底),输入为各类别计数。
func _entropy(counts) -> float:
	var total := 0.0
	for c in counts:
		total += float(c)
	if total <= 0.0:
		return 0.0
	var h := 0.0
	for c in counts:
		var cf := float(c)
		if cf > 0.0:
			var p := cf / total
			h -= p * (log(p) / log(2.0))
	return h


func _write(d: Dictionary) -> void:
	_f.store_line(JSON.stringify(d))
	_f.flush()
