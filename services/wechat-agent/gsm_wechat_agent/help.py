"""Local, accurate help remains available even when the model is offline."""

HELP = """我是 GPU 服务器助手，可以帮你：
• 查资源：现在谁在用 GPU、推荐一张24G显存的GPU
• 管容器：我的容器、启动 test、停止 test、重启 test
• 查日志：查看 test 的日志、test 为什么退出了
• 开环境：创建容器（告诉我名称、镜像、CPU、内存和GPU）
• 任务通知：开启 test 的任务通知、查看 test 的训练任务
• 候补排队：加入24G显存候补、我排第几
• 查健康：服务器1正常吗
• 审批：查看待审批申请（批准和拒绝需要管理员权限）
可以直接用自然语言说需求。停止、重启需要再次确认；我只能操作你有权限的资源。"""

TRAINING_HELP = """训练完成通知需要知道具体进程的退出结果。先说“开启 test 的任务通知”，再在该容器终端把原命令加一个前缀：
python /workspace/.gsm/gsm-run.py python train.py
它会实时显示输出并记录退出码，完成或失败后发微信通知。也可说“查看 test 的训练任务”。已有任务不会自动接管，Notebook 内的单元格也不会自动跟踪。微信长时间未互动时，请先发一条消息恢复通知通道。"""


def help_text(topic=None):
    return TRAINING_HELP if topic == "training" else HELP
