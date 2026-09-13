---
listed: false
name: tool-creator
description: 创建、审查和改进磁盘工具包（dynamic tools）。当需要的能力不在现有工具里时，用它教模型写一个可被 tool(search/activate) 发现并使用的新工具。
---

# Tool Creator — 创建磁盘工具包的元技能

你现在作为 Tool Creator 运行。你的工作是帮用户把「现有工具做不到的事」做成一个磁盘工具包：写完即刻可被 tool(action="search") 发现、tool(action="activate") 装载并调用，不用重启、不用 /new。

## 什么时候用

- 用户要的能力现有工具（含挂起池）都没有，且值得沉淀为可复用工具
- 把一段反复用的脚本/命令固化成工具

什么时候不用：一次性任务直接用 shell/write 完成即可，不必造工具。

## 工具包格式

一个工具包 = 一个目录，放在两处之一

- 用户级 `<coara_home>/users/default/tools/<包名>/`——所有工作空间可见，优先放这里
- 工作空间级 `<工作空间>/.coara/tools/<包名>/`——仅该空间可见

```
my_tool/
├── TOOL.md   # 元数据 + 使用说明
└── main.py   # 入口（可在 TOOL.md 的 entry 字段改名）
```

### TOOL.md

```markdown
---
name: my_tool            # 工具名：字母开头的标识符（字母/数字/下划线），全局唯一，别撞内置工具
description: 一句话说清干什么、什么时候用。这是 LLM 决定用不用它的唯一依据，认真写
entry: main.py           # 可选，默认 main.py
parameters:              # JSON Schema（object 型），没有参数就写 properties: {}
  type: object
  properties:
    path:
      type: string
      description: 要处理的文件绝对路径
  required: [path]
---

正文可选：补充使用说明、注意事项、输出格式约定。activate 后会并入工具描述尾部给 LLM 看。
```

### main.py 入口约定

```python
def run(**params):        # 也支持 async def run(...)
    ...
    return "结果文本"      # 返回 str；dict/list 会自动转 JSON 给 LLM
```

规则

- 参数名与 TOOL.md 的 parameters.properties 一一对应，全部经 **params 传入
- 出错就抛异常（RuntimeError("人类可读的原因")），框架会自动转成工具错误结果
- 返回结果写给 LLM 看：简洁、结构化、包含下一步可用信息（如产出文件的绝对路径）
- 依赖第三方库就 import 时包 try，缺库抛「需要 X 库：pip install X」
- 长时间任务在 docstring 级说明里写清楚，让调用方决定要不要后台跑

## 安全闸（写工具时要知道）

- 自建工具默认每次调用都要用户审批——description 里把副作用写清楚，用户审批时才好判断
- 用户把工具名加进 config.yaml 的 tools.auto_approve 名单后免审批（只建议纯只读工具进名单）
- 工具代码跑在 coara 进程内，别写会拖死事件循环的无限阻塞；重活用 subprocess 或给 async

## 创建流程

1. 跟用户确认这个能力值得固化（一次性任务别造工具）
2. 选位置：通用放用户级，空间专属放工作空间级
3. 写 TOOL.md + main.py（先用 write 落盘）
4. 自检：`tool(action="search", query="")` 应能看到新工具；看不到就看 coara 日志里的加载 warning
5. `tool(action="activate", name="my_tool")` 后用真实参数跑一次冒烟，确认行为正确再交付

## 审查现有工具包时看什么

- description 是否够 LLM 做出使用决策（干什么、何时用、副作用）
- schema 与 run 签名是否一致
- 错误路径是否都会转成人类可读的异常
- 有没有不必要的副作用（写文件、发请求）没写进 description
