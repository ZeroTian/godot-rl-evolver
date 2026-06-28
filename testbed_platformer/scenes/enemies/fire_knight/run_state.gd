extends BaseState
## 行走状态：左右巡逻、遇悬崖掉头；当攻击范围 A1 检测到目标，就切换到攻击状态。

var SPEED: float = Tunables.get_param("enemy_speed", 50.0)

var direct: Vector2 = Vector2.RIGHT

# 状态节点位于 FireKnight/FSM/Run，往上两级即角色根节点
@onready var master: CharacterBody2D = get_parent().get_parent()
@onready var anim: AnimationPlayer = master.get_node("AnimationPlayer")
@onready var sprite: AnimatedSprite2D = master.get_node("AnimatedSprite2D")
@onready var attack_pivot: Node2D = master.get_node("Attack")
@onready var ray_floor_right: RayCast2D = master.get_node("RayFloorRight")
@onready var ray_floor_left: RayCast2D = master.get_node("RayFloorLeft")
@onready var a1: Area2D = master.get_node("Attack/A1")


func enter() -> void:
	anim.play("走动")


func do(delta: float) -> void:
	# 攻击范围里出现目标 → 切到攻击状态（states[1]）
	if a1.get_overlapping_bodies().size() > 0:
		get_parent().change_to(1)
		return

	# 重力
	if not master.is_on_floor():
		master.velocity += master.get_gravity() * delta
	# 水平自走
	master.velocity.x = direct.x * SPEED
	# 悬崖检测 → 掉头
	if direct == Vector2.RIGHT and not ray_floor_right.is_colliding():
		_turn(Vector2.LEFT)
	elif direct == Vector2.LEFT and not ray_floor_left.is_colliding():
		_turn(Vector2.RIGHT)
	master.move_and_slide()


func _turn(new_dir: Vector2) -> void:
	direct = new_dir
	sprite.flip_h = new_dir == Vector2.LEFT
	# 攻击范围（Attack 节点）跟随朝向水平翻转
	attack_pivot.scale.x = -1.0 if new_dir == Vector2.LEFT else 1.0
