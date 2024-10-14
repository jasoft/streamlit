import akshare as ak
import functools
import time
from log import logger


class AkshareWrapper:
    def __init__(self, max_cache_size=128, cache_expire_seconds=60):
        self.cache_expire_seconds = cache_expire_seconds
        self.get_data = functools.lru_cache(maxsize=max_cache_size)(
            self._cached_get_data
        )

    def __getattr__(self, name):
        def wrapper_function(*args, **kwargs):
            return self.get_data_with_cache(name, *args, **kwargs)

        return wrapper_function

    def _cached_get_data(self, function_name, *args, **kwargs):
        # 调用 akshare 中的函数
        function = getattr(ak, function_name)
        return function(*args, **kwargs)

    def get_data_with_cache(self, function_name, *args, **kwargs):
        # 手动控制缓存过期
        cache_info = self.get_data.cache_info()
        if cache_info.hits + cache_info.misses > 0:
            current_time = time.time()
            if not hasattr(self, "_cache_timestamp"):
                self._cache_timestamp = current_time
                if current_time - self._cache_timestamp > self.cache_expire_seconds:
                    self.get_data.cache_clear()
                    self._cache_timestamp = current_time
        logger.info(f"调用 akshare 函数: {function_name} {args} {kwargs}")
        return self.get_data(function_name, *args, **kwargs)


# 示例使用
akshare = AkshareWrapper(cache_expire_seconds=180)
