# template/tunables.gd — Tunables autoload singleton（参数化层）
#
# ★ 用法说明:
#   1. 把本文件复制到你的游戏项目，建议放在 res://rl/Tunables.gd
#   2. 把 tunables.json 复制到 res://rl/tunables.json，按需编辑参数
#   3. 在 project.godot 注册 autoload:
#        [autoload]
#        Tunables="*res://rl/Tunables.gd"
#      或在编辑器: Project → Project Settings → Autoload → 添加 Tunables.gd
#   4. 游戏脚本中把硬编码常量替换为:
#        var gap = Tunables.get("gap_width", 120.0)
#      优化器改 tunables.json 的 value 字段，下次启动自动生效，无需碰 .gd/.tscn
#
# ★ 容错:
#   - 文件缺失 / JSON 解析失败时不崩溃，均返回 default 值
#   - 每次 _ready 加载，热重载场景时自动更新

extends Node

# 存储从 tunables.json 读取的参数字典 {key: value}
var _params: Dictionary = {}

# tunables.json 的路径（相对于 res://）
const _TUNABLES_PATH := "res://rl/tunables.json"


func _ready() -> void:
	_load_tunables()


## 读取参数值，找不到或文件缺失时返回 default。
## 示例: var force = Tunables.get_param("jump_force", 400.0)
## 注意: 不能命名为 get()，会与 GDScript Object 内置方法冲突。
func get_param(key: String, default = null):
	return _params.get(key, default)


## 重新从磁盘加载 tunables.json（运行中热更新用）。
func reload() -> void:
	_load_tunables()


# ---------- 内部 ----------

func _load_tunables() -> void:
	_params.clear()

	# 文件不存在时静默跳过
	if not FileAccess.file_exists(_TUNABLES_PATH):
		push_warning("Tunables: 文件不存在 %s，所有参数将使用默认值" % _TUNABLES_PATH)
		return

	var file := FileAccess.open(_TUNABLES_PATH, FileAccess.READ)
	if file == null:
		push_warning("Tunables: 无法打开 %s (error=%d)" % [_TUNABLES_PATH, FileAccess.get_open_error()])
		return

	var text := file.get_as_text()
	file.close()

	var parsed = JSON.parse_string(text)
	if parsed == null:
		push_warning("Tunables: JSON 解析失败，请检查 %s 格式" % _TUNABLES_PATH)
		return

	# schema: {"version":1, "params": {"key": {"value":..., "range":..., ...}}}
	if not parsed.has("params") or typeof(parsed["params"]) != TYPE_DICTIONARY:
		push_warning("Tunables: tunables.json 缺少 'params' 字段")
		return

	for key in parsed["params"]:
		var entry = parsed["params"][key]
		if typeof(entry) == TYPE_DICTIONARY and entry.has("value"):
			_params[key] = entry["value"]

	# ★ 注意: 优化器只修改 tunables.json 中的 "value" 字段
	#         range/type/desc 是游戏作者契约，优化器不得改动
