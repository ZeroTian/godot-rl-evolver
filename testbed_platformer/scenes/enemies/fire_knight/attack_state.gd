extends BaseState
## 攻击状态：进入时按连击计时器决定播放一段(攻击1)或二段(攻击2)。
## 攻击判定由 AnimationPlayer 的「方法调用轨道」在命中帧回调 attack1 / attack_l / attack_r。
## 攻击动画播完 → 回到行走；若刚打完一段攻击，就开启 Timer 留出一个二段连击窗口。

@onready var master: CharacterBody2D = get_parent().get_parent()
@onready var anim: AnimationPlayer = master.get_node("AnimationPlayer")
@onready var sprite: AnimatedSprite2D = master.get_node("AnimatedSprite2D")
@onready var timer: Timer = $Timer
@onready var a1: Area2D = master.get_node("Attack/A1")
@onready var a2l: Area2D = master.get_node("Attack/A2L")
@onready var a2r: Area2D = master.get_node("Attack/A2R")


func enter() -> void:
	master.velocity.x = 0.0
	# 计时器还在走 = 处在一段攻击后的连击窗口内 → 接二段攻击
	if timer.is_stopped():
		anim.play("攻击1")
	else:
		anim.play("攻击2")


func do(delta: float) -> void:
	# 攻击中原地不动，但保持贴地（继续受重力）
	master.velocity.x = 0.0
	if not master.is_on_floor():
		master.velocity += master.get_gravity() * delta
	master.move_and_slide()


# —— 以下三个函数由 AnimationPlayer 的方法轨道在攻击命中帧调用 ——
func attack1() -> void:
	_hit(a1)


func attack_l() -> void:
	_hit(a2l)


func attack_r() -> void:
	_hit(a2r)


func _hit(area: Area2D) -> void:
	for body in area.get_overlapping_bodies():
		if body.is_in_group("player"):
			body.take_hit(10)


## AnimatedSprite2D 的 animation_finished 信号（在场景中连接到本脚本）
func _on_animated_sprite_2d_animation_finished() -> void:
	match sprite.animation:
		"攻击1":
			timer.start()              # 开启二段连击窗口
			get_parent().change_to(0)  # 回到行走
		"攻击2":
			get_parent().change_to(0)
