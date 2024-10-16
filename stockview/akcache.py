import time
from log import logger


class CacheWrapper:
    def __init__(self, obj, cache_time=180):
        self.obj = obj
        self.cache_time = cache_time
        self.cache = {}

    def __getattr__(self, name):
        method = getattr(self.obj, name)

        def cached_method(*args, **kwargs):
            key = (name, args, tuple(kwargs.items()))  # 创建缓存键
            current_time = time.time()

            # 检查缓存是否存在且未过期
            if key in self.cache:
                cached_result, timestamp = self.cache[key]
                if current_time - timestamp < self.cache_time:
                    logger.debug(f"缓存命中: {name}")
                    return cached_result

            # 如果缓存不存在或过期，调用方法并缓存结果
            logger.debug(f"缓存未命中, 正在调用方法 {name} {args} {kwargs}")
            result = method(*args, **kwargs)
            self.cache[key] = (result, current_time)
            return result

        return cached_method

    def clear_cache(self):
        self.cache.clear()  # 清空缓存
        logger.info("缓存已清空")


# 示例使用
