extends CharacterBody2D
## 近战火焰骑士的角色根脚本：只管血量、分组与受伤接口。
## 移动 / 攻击逻辑在 FSM 子节点的各状态里。

@export var health: int = 100


func _ready() -> void:
	add_to_group("monster")


## 被主角攻击命中：扣血，血量耗尽则销毁
func take_hit(value: int) -> void:
	health -= value
	if health <= 0:
		queue_free()
