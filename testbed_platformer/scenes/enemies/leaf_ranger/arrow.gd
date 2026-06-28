extends Area2D
## 弓箭：远程弓箭手射出的抛射物，生成后沿水平方向匀速飞行。
## （命中主角的伤害逻辑留待主角场景完成后补全，见后续章节。）

@export var speed: float = 260.0

## 飞行方向：+1 向右、-1 向左。由弓箭手在生成弓箭时赋值。
var vec_x: float = 1.0


func _ready() -> void:
	body_entered.connect(_on_body_entered)
	# 没命中任何东西的箭会一直飞行、白白占用内存/CPU；用一个 5 秒计时器让它自毁
	$LifeTimer.timeout.connect(queue_free)
	# 箭素材默认朝右；向左飞时把整个箭（含碰撞框）水平翻转
	if vec_x < 0.0:
		scale.x = -1.0


func _physics_process(delta: float) -> void:
	position.x += vec_x * speed * delta


## 飞行途中撞到主角：造成伤害并销毁自己
func _on_body_entered(body: Node2D) -> void:
	if body.is_in_group("player"):
		body.take_hit(10)
		queue_free()
