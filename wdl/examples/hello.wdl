name: hello
description: 最小两节点串联示例
nodes:
  draft:
    task: 用一句话介绍 WDL 工作流引擎
  polish:
    task: 把这句话润色得更吸引人
    input: "{{steps.draft.text}}"
edges:
  - from: draft
    to: polish
