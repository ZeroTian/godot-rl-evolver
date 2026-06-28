extends BaseState
## 下落状态（FSM 索引 4）：在空中下坠，落地回站立。

const SPEED: float = 160.0

@onready var player: Player = get_parent().get_parent()
@onready var sprite: AnimatedSprite2D = player.get_node("AnimatedSprite2D")
@onready var attack_pivot: Node2D = player.get_node("Attack")


func enter() -> void:
	sprite.play("下落")


func do(delta: float) -> void:
	if player.is_on_floor():
		get_parent().change_to(0)  # Idle
		return
	var axis := Input.get_axis("move_left", "move_right")
	if axis > 0:
		sprite.flip_h = false
		attack_pivot.scale.x = 1.0
	elif axis < 0:
		sprite.flip_h = true
		attack_pivot.scale.x = -1.0
	player.velocity.x = axis * SPEED
	player.velocity += player.get_gravity() * delta
	player.move_and_slide()
