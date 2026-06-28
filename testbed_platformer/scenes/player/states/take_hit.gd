extends BaseState
## 受击状态（FSM 索引 5）：被命中后短暂硬直。
## 受击 / 死亡动画由 Player.take_hit() 触发，动画结束处理也在 Player 里。

@onready var player: Player = get_parent().get_parent()


func enter() -> void:
	player.velocity.x = 0


func do(delta: float) -> void:
	if not player.is_on_floor():
		player.velocity += player.get_gravity() * delta
	player.move_and_slide()
