"""领域错误：命令违反业务规则时抛出，不写入任何事件。"""

from __future__ import annotations


class DomainError(Exception):
    """所有业务规则拒绝的基类。"""


class ContractViolation(DomainError):
    """事件信封不满足交换契约。"""


class EventConflict(DomainError):
    """事件编号已被不同内容占用，已转入争议。"""


class PermissionDenied(DomainError):
    """操作者角色无权执行该命令（如商务代替权利人扩权）。"""


class UnknownAsset(DomainError):
    """素材尚未登记来源。"""


class UnknownGrant(DomainError):
    """引用的授权不存在。"""


class UnknownRemovalCase(DomainError):
    """回执指向的撤展案件不存在。"""


class LicenseNotUsable(DomainError):
    """当前授权不覆盖该渠道或时点（含已撤回、已到期）。"""


class HeritageNoteRequired(DomainError):
    """涉及活态传承的说明缺失或未经权利人确认。"""


class ReleaseFrozen(DomainError):
    """该场次已锁场，当晚实际使用版本不可再变更。"""


class BoothAlreadyConfirmed(DomainError):
    """同一实体展位在同一夜晚已被其他并行方案确认。"""


class ReceiptConflict(DomainError):
    """回执编号相同但素材或范围不同，已转入争议。"""
