extends BaseState
## 起跳状态（FSM 索引 2）：给一个向上初速度，上升到顶（velocity.y>0）转入下落。

const SPEED: float = 160.0
var JUMP_SPEED: float = Tunables.get_param("jump_force", 360.0)

@onready var player: Player = get_parent().get_parent()
@onready var sprite: AnimatedSprite2D = player.get_node("AnimatedSprite2D")
@onready var attack_pivot: Node2D = player.get_node("Attack")


func enter() -> void:
	sprite.play("起跳")
	player.velocity.y = -JUMP_SPEED


func do(delta: float) -> void:
	if player.velocity.y > 0:
		get_parent().change_to(4)  # Fall
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
