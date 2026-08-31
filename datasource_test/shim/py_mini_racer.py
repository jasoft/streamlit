from mini_racer import MiniRacer as _MR
class MiniRacer(_MR):
    def __getattr__(self, name):
        return getattr(_MR, name)
