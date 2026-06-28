extends CharacterBody2D
class_name Player
## 主角：由状态机驱动（站立/走动/起跳/下落/攻击/受击）。
## 这里只放跨状态共享的数据与对外接口：血量、受伤、死亡信号。

signal died  ## 死亡时发出，留给后续（游戏结束 / 重生）使用

@export var health: int = 100

@onready var sprite: AnimatedSprite2D = $AnimatedSprite2D
@onready var fsm: FSM = $FSM
@onready var health_bar = get_node_or_null("CanvasLayer/HealthBar")

var dying: bool = false  ## 已进入死亡流程，屏蔽后续伤害，避免重复发 died


func _ready() -> void:
	add_to_group("player")
	if health_bar:
		health_bar.max_value = health
		health_bar.value = health


## 受到伤害：扣血、更新血条、切到受击状态（索引 5）并播对应动画。
## 血量耗尽时播死亡动画，停留 3 秒让玩家看清死亡状态，再发 died 信号并销毁。
func take_hit(value: int) -> void:
	if dying:
		return
	health -= value
	if health_bar:
		health_bar.value = max(health, 0)
	fsm.change_to(5)  # TakeHit
	if health > 0:
		sprite.play("受击")
	else:
		dying = true
		sprite.play("死亡")
		await get_tree().create_timer(3.0).timeout
		died.emit()
		queue_free()


## AnimatedSprite2D.animation_finished 连到此：受击→站立（死亡由 take_hit 里延时处理）
func _on_sprite_animation_finished() -> void:
	if sprite.animation == "受击":
		fsm.change_to(0)
