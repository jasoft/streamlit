import time
import hashlib
from functools import wraps
from stockview.log import logger


class RateLimitedCache:
    """限流缓存包装器，防止API被封"""

    def __init__(self, cache_time=180):
        self.cache_time = cache_time
        self.cache = {}
        self.call_times = {}

    def __call__(self, func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # 生成缓存键
            key = self._make_key(func.__name__, args, kwargs)
            current_time = time.time()

            # 检查缓存是否存在且未过期
            if key in self.cache:
                cached_result, timestamp = self.cache[key]
                if current_time - timestamp < self.cache_time:
                    logger.debug(f"[RateLimiter] 缓存命中: {func.__name__}")
                    return cached_result

            # 检查是否在限流时间内
            if key in self.call_times:
                elapsed = current_time - self.call_times[key]
                if elapsed < 2:  # 最少间隔2秒
                    logger.debug(f"[RateLimiter] 请求过于频繁，等待 {2-elapsed:.1f}s")
                    time.sleep(2 - elapsed)

            # 调用函数
            logger.debug(f"[RateLimiter] 调用: {func.__name__}")
            self.call_times[key] = time.time()
            result = func(*args, **kwargs)

            # 缓存结果
            self.cache[key] = (result, time.time())
            return result

        return wrapper

    def _make_key(self, func_name, args, kwargs):
        """生成缓存键"""
        key_parts = [func_name] + [str(a) for a in args]
        if kwargs:
            key_parts.extend([f"{k}={v}" for k, v in sorted(kwargs.items())])
        return "|".join(key_parts)

    def clear_cache(self):
        """清空缓存"""
        self.cache.clear()
        self.call_times.clear()
        logger.info("[RateLimiter] 缓存已清空")


# 创建全局实例
rate_limiter = RateLimitedCache(cache_time=180)
