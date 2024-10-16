import coloredlogs
import logging

coloredlogs.install(level="INFO")

logger = logging.getLogger("streamlit.stockview")
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("fsevents").setLevel(logging.WARNING)
# 配置日志记录
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# 添加一个文件处理器，将日志写入文件
file_handler = logging.FileHandler("stockview.log")
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(
    logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
)
logger.addHandler(file_handler)
