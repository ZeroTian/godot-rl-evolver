extends Node2D
## 真游戏 RL 训练根：把真地面/主角/真敌人 + Sync + Agent 拼到一起。
## 负责 episode 复位（主角归位 + 敌人重生 + 清箭）和给 Agent 提供敌人/缺口/血量查询。
## 不修改任何真游戏脚本——全靠在外部读写节点状态。

const FK := preload("res://scenes/enemies/fire_knight/fire_knight.tscn")
const LR := preload("res://scenes/enemies/leaf_ranger/leaf_ranger.tscn")

const PLAYER_START := Vector2(80, -30)
const FK_START := Vector2(780, -30)   # 缺口对岸实地中段:落地后有空间走过去正面打(不贴坑边,避免被打退摔进缺口)
const LR_START := Vector2(900, -30)

@onready var player: CharacterBody2D = $Player
@onready var ground: TileMapLayer = $Ground
@onready var agent: AIController2D = $Agent


func _ready() -> void:
	agent.bind(player, self)
	_reset_to_start()


func reset_episode() -> void:
	_reset_to_start()


func _reset_to_start() -> void:
	# 松开所有虚拟按键
	for a in ["move_left", "move_right", "jump", "attack"]:
		Input.action_release(a)

	# 主角归位
	player.global_position = PLAYER_START
	player.velocity = Vector2.ZERO
	player.health = 100
	player.dying = false
	if player.health_bar:
		player.health_bar.value = 100
	player.fsm.change_to(0)   # 回到 Idle

	# 清掉现存敌人与飞行中的箭，重新生成两个满血敌人
	for m in get_tree().get_nodes_in_group("monster"):
		m.queue_free()
	for c in get_children():
		if c is Area2D and "vec_x" in c:   # 箭(arrow.gd 有 vec_x)，挂在地图根下
			c.queue_free()

	var fk := FK.instantiate()
	fk.position = FK_START
	fk.health = 40   # 训练用:降低血量(2 击致死)→ 让"击杀"更容易被发现,闭环演示能在有限步数内收敛
	add_child(fk)
	# 战斗演示:聚焦单个火骑士(挡在缺口对岸实地),先不放弓箭手，便于在有限训练内学会清怪


# ---------- 给 Agent 的查询 ----------

func _live_monsters() -> Array:
	var out := []
	for m in get_tree().get_nodes_in_group("monster"):
		if not m.is_queued_for_deletion():
			out.append(m)
	return out


func monster_count() -> int:
	return _live_monsters().size()


func nearest_monster(from: Vector2) -> Node2D:
	var best: Node2D = null
	var bd := 1.0e9
	for m in _live_monsters():
		var d: float = abs(m.global_position.x - from.x)
		if d < bd:
			bd = d
			best = m
	return best


func player_hp() -> int:
	return player.health


## 现存敌人血量总和(用于「造成伤害」的密集奖励:agent 打掉敌人血就给分)
func total_monster_hp() -> int:
	var s := 0
	for m in _live_monsters():
		s += m.health
	return s


## 主角前方一格、脚下是否有地块（false=缺口）。靠查询 TileMapLayer，与瓦片尺寸无关。
func gap_ahead(player_pos: Vector2) -> float:
	var ahead := player_pos + Vector2(28, 24)
	var cell := ground.local_to_map(ground.to_local(ahead))
	return 0.0 if ground.get_cell_source_id(cell) == -1 else 1.0
