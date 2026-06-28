extends BaseState
## 攻击状态（FSM 索引 3）：三段连击。按 attack_index 播攻击1/2/3，
## 由 AnimationPlayer 方法轨道在命中帧调 attack_check() 判定伤害；
## 攻击动画播完 index 进位并开 Timer；Timer 超时把 index 归 1（连击中断）。

@onready var player: Player = get_parent().get_parent()
@onready var sprite: AnimatedSprite2D = player.get_node("AnimatedSprite2D")
@onready var anim: AnimationPlayer = player.get_node("AnimationPlayer")
@onready var timer: Timer = $Timer
@onready var a1: Area2D = player.get_node("Attack/A1")
@onready var a2: Area2D = player.get_node("Attack/A2")
@onready var a3: Area2D = player.get_node("Attack/A3")

var attack_index: int = 1


func enter() -> void:
	timer.stop()
	player.velocity.x = 0
	match attack_index:
		1:
			anim.play("攻击1")
		2:
			anim.play("攻击2")
		3:
			anim.play("攻击3")


func do(delta: float) -> void:
	player.velocity.x = 0
	if not player.is_on_floor():
		player.velocity += player.get_gravity() * delta
	player.move_and_slide()


func exit() -> void:
	anim.stop()
	sprite.frame = 0


## AnimationPlayer 方法轨道在命中帧调用：按当前段数取对应攻击区域，伤害怪物
func attack_check() -> void:
	var bodies: Array = []
	match attack_index:
		1:
			bodies = a1.get_overlapping_bodies()
		2:
			bodies = a2.get_overlapping_bodies()
		3:
			bodies = a3.get_overlapping_bodies()
	for b in bodies:
		if b.is_in_group("monster"):
			b.take_hit(20)


## sprite.animation_finished 连到此（仅处理攻击动画）：进位段数、开连击窗口、回站立
func _on_sprite_animation_finished() -> void:
	if sprite.animation.begins_with("攻击"):
		attack_index += 1
		if attack_index > 3:
			attack_index = 1
		timer.start()
		get_parent().change_to(0)  # Idle


## Timer 超时：连击窗口结束，重置回第一段
func _on_timer_timeout() -> void:
	attack_index = 1
