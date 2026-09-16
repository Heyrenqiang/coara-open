"""工作空间内的固定落点约定（三端共用一处真源）。

端上发来的附件只有一处落点：``<workspace>/<UPLOAD_DIR_NAME>``。手机端
（matrix m.file 入站）与 web 端（``/api/upload``）都写这里，视图与前端按
同一个相对路径取文件；不要再出现第二个上传目录。
"""

from __future__ import annotations

UPLOAD_DIR_NAME = "uploads"
