import logging
import sys

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("/tmp/log/auto_delete_tasks.log", mode='w'),
        logging.StreamHandler()
    ]
)

# 主流程
# 仅支持迅雷下载器；迅雷无需执行自动删除已完成任务，程序退出
logging.info("当前仅支持迅雷下载器，无需执行自动删除已完成任务，程序退出。")
sys.exit(0)
