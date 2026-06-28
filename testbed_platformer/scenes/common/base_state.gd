extends Node
class_name BaseState
## 状态基类：所有具体状态继承它，按需重写下面三个生命周期函数。
## 主角与怪物的状态机共用这一套接口（进入 / 运行中 / 退出）。

## 进入该状态时调用一次（例如切换动画）
func enter() -> void:
	pass

## 该状态每物理帧调用（执行功能，并检测是否需要转换到别的状态）
func do(_delta: float) -> void:
	pass

## 离开该状态时调用一次（清理本状态对角色的影响）
func exit() -> void:
	pass
