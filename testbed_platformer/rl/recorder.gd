extends Node
## 推理时录制截图，按 episode 分文件夹：rec/epNNN/fMMMM.png。
## 通过「主角 x 突然跳回起点(<120)」检测一局结束→新建文件夹，
## 这样能从中挑出「完整通关」的一整段连续帧做 GIF。
## headless 训练时自动跳过(无画面)。

var ep_idx: int = 0
var frame_in_ep: int = 0
var rcount: int = 0
var last_x: float = 0.0
var player: Node2D = null


func _ready() -> void:
	if DisplayServer.get_name() == "headless":
		return
	# 找到主角(map 根下的 Player)
	player = get_node_or_null("../Player")
	_new_ep()


func _new_ep() -> void:
	ep_idx += 1
	frame_in_ep = 0
	DirAccess.make_dir_recursive_absolute(ProjectSettings.globalize_path("res://rec/ep%03d" % ep_idx))


func _process(_delta: float) -> void:
	if DisplayServer.get_name() == "headless" or player == null:
		return
	var x: float = player.global_position.x
	# 主角从右侧跳回起点 → 上一局结束，开新文件夹
	if x < 120.0 and last_x > 200.0:
		_new_ep()
	last_x = x

	rcount += 1
	if rcount % 2 != 0:      # 每 2 个渲染帧截一张(speedup=8 下尽量密)
		return
	var img := get_viewport().get_texture().get_image()
	img.save_png("res://rec/ep%03d/f%04d.png" % [ep_idx, frame_in_ep])
	frame_in_ep += 1
