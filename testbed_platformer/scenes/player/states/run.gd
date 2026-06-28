extends BaseState
## 走动状态（FSM 索引 1）：左右移动，并随时分流到跳跃 / 攻击 / 下落 / 站立。

const SPEED: float = 160.0

@onready var player: Player = get_parent().get_parent()
@onready var sprite: AnimatedSprite2D = player.get_node("AnimatedSprite2D")
@onready var attack_pivot: Node2D = player.get_node("Attack")


func enter() -> void:
	sprite.play("走动")


func do(delta: float) -> void:
	if not player.is_on_floor():
		get_parent().change_to(4)  # Fall
		return
	if Input.is_action_just_pressed("jump"):
		get_parent().change_to(2)  # Jump
		return
	if Input.is_action_just_pressed("attack"):
		get_parent().change_to(3)  # Attack
		return
	var axis := Input.get_axis("move_left", "move_right")
	if axis == 0:
		get_parent().change_to(0)  # Idle
		return
	_face(axis)
	player.velocity.x = axis * SPEED
	player.velocity += player.get_gravity() * delta
	player.move_and_slide()


## 根据移动方向翻转精灵与攻击范围
func _face(axis: float) -> void:
	if axis > 0:
		sprite.flip_h = false
		attack_pivot.scale.x = 1.0
	else:
		sprite.flip_h = true
		attack_pivot.scale.x = -1.0
