extends Node
class_name FSM
## 状态机管理器：挂在 FSM 节点上，它的每个直接子节点就是一个状态。
## 职责：保存所有状态、每物理帧驱动「当前状态」、以及在状态之间切换。
## 主角和怪物共用此管理器。

## 所有状态（顺序即索引）
var states: Array = []
## 当前状态
var current: BaseState


func _ready() -> void:
	states = get_children()
	current = states[0]
	current.enter()


func _physics_process(delta: float) -> void:
	current.do(delta)


## 切换到 states[id]：旧状态 exit → 替换 current → 新状态 enter
func change_to(id: int) -> void:
	current.exit()
	current = states[id]
	current.enter()
