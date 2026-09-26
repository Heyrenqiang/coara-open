name: write-note
description: 工具循环示例：节点声明 write_file，把产出的短句写入文件
settings:
  max_tool_rounds: 10
  shell_timeout: 60
nodes:
  write:
    task: |
      想一句关于工作流的短句，然后调用 write_file 把它写入 note.txt。
      写入成功后，最终回复一行：已写入 note.txt 以及短句原文
    tools: [write_file]
edges: []
