from time import time_ns


class Message:
    def __init__(self, timestamp: int, role: str, message: str) -> None:
        self.timestamp = timestamp
        self.role = role
        self.message = message

    def __init__(self, role: str, message: str) -> None:
        self.timestamp = time_ns()
        self.role = role
        self.message = message

    def __str__(self) -> str:
        return f"{self.role} @ {self.timestamp} : {self.message}"
