# PROVENANCE — testbed_platformer 来源与许可证说明

## 来源

- **源路径**：`/mnt/e/code/godot-study/platformer`（本机 godot-study 教程仓库）
- **复制日期**：2026-06-28
- **源版本说明**：跟随 P33 Godot 4 教程实战建立的平台跳跃游戏，在 godot-study 仓库中由
  用户 ZeroTian 独立编写（GDScript 脚本、场景结构、RL 接入层均为原创代码）。

## 许可证扫描结果

```
find /mnt/e/code/godot-study/platformer -maxdepth 3 \
  \( -iname 'license*' -o -iname 'copying*' -o -iname 'credits*' \) -print
```

**结果：无输出**——源项目目录内未找到任何 LICENSE / COPYING / CREDITS 文件。

## 第三方素材处理

源项目使用了来自 itch.io 的免费像素素材（见源仓 `ASSETS.md`），涉及 5 套资产：

| 素材 | 原作者 | itch.io 页面 | 原路径 |
|------|--------|-------------|--------|
| 主角动画 | rvros | https://rvros.itch.io/animated-pixel-hero | `assets/player/` |
| 火焰骑士 | chierit | https://chierit.itch.io/elementals-fire-knight | `assets/enemies/fire-knight/` |
| 树叶游侠 | chierit | https://chierit.itch.io/elementals-leaf-ranger | `assets/enemies/leaf-ranger/` |
| 地图 tileset | anokolisa | https://anokolisa.itch.io/moon-graveyard | `assets/tilemap/` |
| UI 血条 | adwitr | https://adwitr.itch.io/pixel-health-bar-asset-pack-2 | `assets/ui/` |

上述素材均属"免费版本/随心付"下载，**源仓 ASSETS.md 明确标注"商用前请到各自页面确认授权"**，
未附明确的再分发许可条款（无 CC / MIT / 公共域声明）。依据计划 Global Constraints：
**无明确再分发许可的第三方二进制素材不得入库**。

### 替换方案

所有受版权保护的图片均已替换为**仓内自有 grey-box 占位图**，由本次脚本程序化生成（纯色矩形 PNG），
不含任何受版权素材像素：

| 占位图 | 尺寸 | 颜色 | 替代对象 |
|--------|------|------|----------|
| `assets/grey_box/player.png` | 32×32 | 蓝色 `#46_82_C8` | 主角所有动画帧 |
| `assets/grey_box/fire_knight.png` | 32×32 | 红色 `#C8_50_3C` | 火骑士所有动画帧 |
| `assets/grey_box/tile.png` | 16×16 | 灰褐色 `#78_6E_64` | 地图 tileset |
| `assets/grey_box/background.png` | 640×360 | 深蓝 `#1E_1E_32` | 训练场背景 |

修改的资源文件：
- `scenes/player/player_frames.tres` — 所有帧引用改为 `res://assets/grey_box/player.png`
- `scenes/enemies/fire_knight/fire_knight_frames.tres` — 同上改为 `fire_knight.png`
- `scenes/map/tileset.tres` — texture 改为 `res://assets/grey_box/tile.png`
- `rl/train_map.tscn` — 背景 Sprite2D texture 改为 `res://assets/grey_box/background.png`

## 原创代码（已入库）

以下文件为用户 ZeroTian 原创 GDScript / 场景结构，无第三方版权问题，可自由入库：

- `rl/game_env.gd`、`rl/game_agent.gd`、`rl/telemetry.gd`、`rl/recorder.gd`
- `rl/train_map.tscn`（场景结构原创，仅背景贴图已替换）
- `scenes/` 下所有 `.gd` 脚本及 `.tscn` 场景（火骑士、玩家、地图等游戏逻辑）
- `addons/godot_rl_agents/` — godot-rl 插件（MIT License，见插件目录内 LICENSE 文件）
