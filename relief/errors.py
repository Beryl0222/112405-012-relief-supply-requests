"""业务办理过程中可向调用方说明的错误。

这些错误都携带可读原因，对应需求中“仓库能解释分配为何可行、哪些澄清仍会改变安排”。
"""
from __future__ import annotations


class ReliefError(Exception):
    """所有业务错误的基类。"""


class UnknownSite(ReliefError):
    """文本中的地点无法对应到登记对象。"""


class UnknownMaterial(ReliefError):
    """文本中的物资无法对应到登记目录。"""


class Ambiguous(ReliefError):
    """数量或对象语义不明，需要按澄清处理而不是猜测。"""

    def __init__(self, question: str, source: str, reason: str = ""):
        super().__init__(question)
        self.question = question
        self.source = source
        self.reason = reason


class BusinessRule(ReliefError):
    """违反业务规则（型号不兼容、容量不足、重复占用等）。"""


class DuplicateCommand(ReliefError):
    """命令幂等键已存在，重复提交直接返回既有结果。"""

    def __init__(self, command_id: str, event_ids: list[str]):
        super().__init__(f"命令 {command_id} 已办理过")
        self.command_id = command_id
        self.event_ids = event_ids
