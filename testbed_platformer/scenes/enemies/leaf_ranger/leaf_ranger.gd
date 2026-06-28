extends CharacterBody2D
## 远程弓箭手：自行左右巡逻，遇悬崖自动掉头，前方发现主角则原地射箭。
## 受伤 / 死亡逻辑留待主角场景完成后补全（见第34节及之后）。

const SPEED: float = 60.0
## 攻击动画里"放箭"的那一帧（从 0 计数）。1_atk 的第 7 张图正是箭离手的瞬间。
const SHOOT_FRAME: int = 6
const ARROW := preload("res://scenes/enemies/leaf_ranger/arrow.tscn")

@export var health: int = 100

## 当前移动方向，初始向右。
var direct: Vector2 = Vector2.RIGHT
## 是否处于攻击中（攻击期间停下，且避免重复触发攻击）。
var is_attacking: bool = false

@onready var anim: AnimatedSprite2D = $AnimatedSprite2D
@onready var ray_floor_right: RayCast2D = $RayFloorRight
@onready var ray_floor_left: RayCast2D = $RayFloorLeft
@onready var ray_attack_right: RayCast2D = $RayAttackRight
@onready var ray_attack_left: RayCast2D = $RayAttackLeft


func _ready() -> void:
	add_to_group("monster")
	anim.play("移动")


func _physics_process(delta: float) -> void:
	# 攻击中：停住，后续交给动画信号驱动（射箭 / 收尾）
	if is_attacking:
		return

	# 重力
	if not is_on_floor():
		velocity += get_gravity() * delta

	# 水平自走
	velocity.x = direct.x * SPEED

	# 悬崖检测：脚前的射线探不到地面 → 掉头
	if direct == Vector2.RIGHT and not ray_floor_right.is_colliding():
		_turn(Vector2.LEFT)
	elif direct == Vector2.LEFT and not ray_floor_left.is_colliding():
		_turn(Vector2.RIGHT)

	# 攻击检测：前方射线打到目标 → 进入攻击
	if direct == Vector2.RIGHT and ray_attack_right.is_colliding():
		_start_attack()
	elif direct == Vector2.LEFT and ray_attack_left.is_colliding():
		_start_attack()

	move_and_slide()


func _turn(new_dir: Vector2) -> void:
	direct = new_dir
	anim.flip_h = new_dir == Vector2.LEFT


func _start_attack() -> void:
	is_attacking = true
	velocity.x = 0.0
	anim.play("攻击")


## 逐帧信号：攻击动画播到"放箭帧"时，生成一支箭
func _on_animated_sprite_2d_frame_changed() -> void:
	if anim.animation == "攻击" and anim.frame == SHOOT_FRAME:
		_shoot()


func _shoot() -> void:
	var arrow := ARROW.instantiate()
	arrow.vec_x = direct.x
	arrow.global_position = ($R if direct == Vector2.RIGHT else $L).global_position
	# 加到父节点（场景根）下，使箭独立于弓箭手存在
	get_parent().add_child(arrow)


## 动画播完信号：攻击动画结束 → 回到巡逻
func _on_animated_sprite_2d_animation_finished() -> void:
	if anim.animation == "攻击":
		is_attacking = false
		anim.play("移动")


## 被主角攻击命中：扣血，血量耗尽则销毁
func take_hit(value: int) -> void:
	health -= value
	if health <= 0:
		queue_free()
