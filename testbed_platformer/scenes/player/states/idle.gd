extends BaseState
## 站立状态（FSM 索引 0）：等待输入，分流到走动 / 跳跃 / 攻击 / 下落。

@onready var player: Player = get_parent().get_parent()
@onready var sprite: AnimatedSprite2D = player.get_node("AnimatedSprite2D")


func enter() -> void:
	sprite.play("站立")


func do(delta: float) -> void:
	if not player.is_on_floor():
		get_parent().change_to(4)  # Fall
		return
	if Input.get_axis("move_left", "move_right") != 0:
		get_parent().change_to(1)  # Run
		return
	if Input.is_action_just_pressed("jump"):
		get_parent().change_to(2)  # Jump
		return
	if Input.is_action_just_pressed("attack"):
		get_parent().change_to(3)  # Attack
		return
	player.velocity.x = 0
	player.velocity += player.get_gravity() * delta
	player.move_and_slide()
