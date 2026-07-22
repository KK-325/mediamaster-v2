import logging
import sys

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("/tmp/log/download_task_adder.log", mode='w'),
        logging.StreamHandler()
    ]
)

def main():
    # 仅支持迅雷下载器；迅雷通过 Selenium 远程添加任务，本脚本无需处理
    logging.info("当前仅支持迅雷下载器，无需通过本脚本添加下载任务。")
    sys.exit(0)

if __name__ == "__main__":
    main()
