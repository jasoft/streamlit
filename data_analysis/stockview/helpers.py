import pytz
from datetime import datetime


class MarketTimeHelper:
    def __init__(self, timezone="Asia/Shanghai"):
        self.tz = pytz.timezone(timezone)

    def during_market_time(self, current_time):
        current_time_gmt8 = current_time.astimezone(self.tz)
        market_open_time, _, _, market_close_time = self._get_market_times(
            current_time_gmt8
        )

        return market_open_time <= current_time_gmt8 < market_close_time

    def minutes_since_market_open(self, current_time):
        current_time_gmt8 = current_time.astimezone(self.tz)
        market_open_time, lunch_start_time, lunch_end_time, _ = self._get_market_times(
            current_time_gmt8
        )

        if current_time_gmt8 < market_open_time:
            return 0
        elif current_time_gmt8 < lunch_start_time:
            delta = current_time_gmt8 - market_open_time
            return int(delta.total_seconds() // 60)
        elif current_time_gmt8 < lunch_end_time:
            return 120
        else:
            delta = current_time_gmt8 - lunch_end_time
            return 120 + int(delta.total_seconds() // 60)

    def _get_market_times(self, current_time_gmt8):
        market_open_time = self.tz.localize(
            datetime.combine(
                current_time_gmt8.date(), datetime.strptime("09:30", "%H:%M").time()
            )
        )
        lunch_start_time = self.tz.localize(
            datetime.combine(
                current_time_gmt8.date(), datetime.strptime("11:30", "%H:%M").time()
            )
        )
        lunch_end_time = self.tz.localize(
            datetime.combine(
                current_time_gmt8.date(), datetime.strptime("13:00", "%H:%M").time()
            )
        )
        market_close_time = self.tz.localize(
            datetime.combine(
                current_time_gmt8.date(), datetime.strptime("15:00", "%H:%M").time()
            )
        )
        return market_open_time, lunch_start_time, lunch_end_time, market_close_time


# 创建 MarketTimeHelper 实例
market_time_helper = MarketTimeHelper()


# 修改相关函数调用
def during_market_time(current_time):
    return market_time_helper.during_market_time(current_time)


def minutes_since_market_open(current_time):
    return market_time_helper.minutes_since_market_open(current_time)


def color_text(s, cap):
    if cap():
        return f":red[{s}]"
    else:
        return f":green[{s}]"
