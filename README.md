# fishdraw — QQ 肥鱼聊天机器人 · 本地绘画实现插件

让 QQ 机器人「肥鱼」在聊天里接单画图：**消息进来 → 排队 → 本地流水线出图 → 图发回会话**。
整条链路对模型是黑匣——它只负责把话递出去、把图发回来，看不到提示词去向、加工过程和画面本身。

系统由三块组成：

| 组件 | 位置 | 干什么 |
|---|---|---|
| `fish-relay` 插件 | `plugin/fish-relay/` | dsh-onebot-qq 的 cordis 子插件：关键词拦截、回执、发图回聊 |
| `fishdraw` 入口壳 | `bin/fishdraw` | 唯一调用入口：配额 → **排队（取号票+画图锁）** → fishpipe |
| `fishpipe` 流水线 | `painter/` | 写词加工 → 两遍出图（ComfyUI）→ 留档 → 发图脚本 |

## 工作流程

```
QQ 消息「画一个 XXX」
   │
   ├─（路 A：关键词）fish-relay 插件 intercept 阶段命中前缀
   │      → ev.stopped = true，消息不进模型（零 token）
   │      → 回执「收到，画着呢」→ spawn fishdraw
   │
   └─（路 B：自然语言）模型在会话里判断用户要画图
          → 自己跑 fishdraw "... " --user <对方QQ> --chat <当前会话>
          （两条路汇到同一个 fishdraw，排同一个队）
                           │
                    drawquota 配额检查（被挡的不进队）
                           │
                    取号票 <seq>-<pid>.ticket（先清死票）
                           │
                    打印 QUEUE <n>（n = 还在画 + 在等的单数）
                    n>0 时：插件路自己发「前面排队n人」；
                            模型路由 fishdraw 经 --chat 走 NapCat 代发
                           │
                    flock 画图锁：上一单没画完不跑下一单
                           │
                    fishpipe run --text "XXX" --user <QQ>
                       ├─ 写词关 fishwords（--no-writer 可跳）
                       ├─ 拼默认值 suffix.json（仅站长单）
                       ├─ 第一遍 MOSS 构图 → 第二遍 Illustrious 换画风
                       │   （--no-stage2 可只出第一遍）
                       ├─ 成品落 painter/out/ + logs/ 留档
                       └─ stdout: DONE <图路径> <字节数>
                           │
                    插件：成品拷进 dataDir/fish-relay/（容器映射区）
                    → bot.sendMedia 发回原会话 → 删暂存
```

## 两条触发方式

### 路 A — 关键词「画一个」（零 token，不经过模型）

消息以 `画一个` 开头即被插件拦下，后面的整段话原样递给流水线：

```
画一个 雨夜的猫娘
画一个 修改 一只穿女仆装的鲸鱼      ← 「修改」= 过写词关加工提示词
画一个 加工 星空下的鲸鱼            ← 「加工」= 第一遍之后再画第二遍
画一个 修改 加工 柴犬在雪地里       ← 两个都开
```

- 旗标「修改 / 加工」顺序不限，后面要带分隔符（空格 / `：` / `，`）；`画一个 修改后的猫` 这种正文里恰好带字的不会被吃掉
- 光秃秃一句「画一个」不算触发，放行给模型正常聊天
- 群里默认要 @ 机器人才接（`atRequiredInGroup`），私聊直接接
- 模型全程不知道发生了什么：消息没进它的上下文，图是 CQ 码直发

### 路 B — 自然语言（模型走 agent 工具）

用户正常说话（「帮我画个…」「能不能来一张…」），模型在会话里调 fishdraw：

```bash
fishdraw "用户的原话" --user <对方QQ号> --chat group:<群号>   # 群聊
fishdraw "用户的原话" --user <对方QQ号> --chat private:<QQ号> # 私聊
```

- `--user` 记配额账（非站长每天 5 张）；`--chat` 让排队通知能直接发到会话——模型在命令跑完之前发不了言，排队播报只能由脚本代发
- 配额挡回、排队位次、DONE/FAIL 都写在 stdout，模型看回执转述，看不到词和图

## 排队机制（fishdraw 内建，两条路共用）

- 票在 `state/draw-queue/`：`取号 → 报位次 → flock 等锁 → 画 → 撕票`
- 死票自动清：持票进程消失或票龄超 6 小时的票不计数
- 被 SIGKILL 掐掉的单靠死票清理兜底，不会堵死队伍
- 排队上限 `FISH_QUEUE_WAIT` 秒（默认 21600），超时自动撤单

## 安装

前置：

1. **dsh-onebot-qq 机器人在跑**（cordis 插件内核 + NapCat/OneBot 协议端，docker 容器把 `dataDir` 映射到 `/app/plugin-workspace`）
2. **ComfyUI**（默认 `127.0.0.1:8188`，`fishpipe` 不在会自动拉）
3. 模型文件就位（见下「使用模型」）

部署：

```bash
# 1. 插件文件放进包内插件目录（目录自动发现，无需注册）
cp -r plugin/fish-relay <dsh-onebot-qq包>/plugins/fish-relay

# 2. 入口壳与流水线放到位（推荐整仓库结构放 ~/ai/ 下，或任意位置+env 指路）
cp -r bin/ painter/ <部署目录>/

# 3. 配置（见下「环境变量」）：至少要给 FISH_OWNER 和 NAPCAT_TOKEN

# 4. 重启机器人进程（插件目录只在启动时扫描一次）
```

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `FISH_OWNER` | `0`（未设） | 站长 QQ：不限配额、单子里拼 `suffix.json` 默认词 |
| `NAPCAT_URL` / `NAPCAT_TOKEN` | `http://127.0.0.1:3000` / 空 | OneBot HTTP 端点与 token（`--chat` 代发、`send` 返图要用） |
| `FISH_QQ_CONFIG` | `~/.dsh/dsh-onebot-qq/config.json` | 备用读 napcat 配置的位置 |
| `FISH_QQ_DATA_DIR` / `FISH_QQ_CONTAINER_DIR` | `~/.dsh/dsh-onebot-qq` / `/app/plugin-workspace` | 发图白名单目录（宿主→容器映射） |
| `FISH_QUEUE_DIR` / `FISH_QUEUE_WAIT` | `~/.local/state/fishdraw/draw-queue` / `21600` | 队列目录 / 等锁上限秒 |
| `FISH_QUOTA_STORE` | `~/.local/state/fishdraw/draw_quota.json` | 配额账本 |
| `FISHPIPE` / `FISH_QUOTA_BIN` / `FISHWORDS_BIN` | 脚本相对路径 | 各组件位置覆盖 |
| `DEEPSEEK_API_KEY` | — | 写词关 API 回退路用 |

插件配置（实例 `pluginConfig["fish-relay"]`，控制台可热改）：

`enabled` `keyword`（默认「画一个」）`atRequiredInGroup` `allowedGroups` `masterOnly` `timeoutSec` `ack`

## 使用模型

流水线在 ComfyUI 上跑两遍（payload 写在 `painter/fishpipe.py`，可自行换）：

| 遍 | 节点 | 模型文件（放 ComfyUI 对应目录） | 作用 |
|---|---|---|---|
| 第一遍 | `UNETLoader` | `moss_image_sft_int8_convrot.safetensors` | 按自然语言提示词构图 |
| | `CLIPLoader` | `qwen3.5_9b_w4a8_gptq.safetensors`（type: `moss_image`） | 文本编码 |
| | `VAELoader` | `flux2-vae.safetensors` | 解码 |
| 第二遍 | `CheckpointLoaderSimple` | `Illustrious-XL-v1.0.safetensors` | 图生图换画风（danbooru 标签，denoise 默认 0.45） |

写词关（`fishwords`）：把用户一句话翻成两台模型各自吃得下的词（moss 自然语言 / danbooru 标签），规则手册在 `painter/painter.md`；走本地 dsh headless 会话，失败回退 `api.deepseek.com`（要 `DEEPSEEK_API_KEY`）。

## 文件清单

```
plugin/fish-relay/index.js   # QQ 插件：关键词拦截 → fishdraw → 发图回聊
bin/fishdraw                 # 唯一入口：配额 → 排队 → fishpipe
bin/drawquota                # 配额账本（非站长 5 张/天）
bin/fishwords                # 写词关：原话 → moss/tags/negative JSON
painter/fishpipe.py          # 流水线：写词 → 拼默认值 → 两遍出图 → 留档 → 返图
painter/painter.md           # 写词手册（写词关照它写）
painter/suffix.example.json  # 默认后缀示例（仅站长单拼）
```

## 黑匣纪律

- 递出去的话不进模型上下文；写词结果只报各段长度，不回读原文
- 成品图不打开、不预览、不描述；只认「成没成、在哪、多大」的回执
- 完整提示词全文只进 `painter/logs/` 留档，不给模型留任何读取口子

## License

MIT
