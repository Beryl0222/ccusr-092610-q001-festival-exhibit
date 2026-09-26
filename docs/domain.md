# 领域约定

香港红磡举办第二届华服嘉年华，活动把自贡花灯技艺、航天展品、中秋文化、英歌舞与人形机器人、学生粤语朗诵和无人机灯光秀汇聚在同一场域。

聚合对象包括`asset_license`、`event_release`、`partner_use`、`removal_case`。事件类型包括`LICENSE_GRANTED`、`USE_RECORDED`、`EVENT_FROZEN`、`LICENSE_WITHDRAWN`、`REMOVAL_CONFIRMED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `LICENSE_GRANTED`：载荷还需包含 `right_holder`, `scope`。
- `EVENT_FROZEN`：载荷还需包含 `release_version`, `frozen_at`。
- `LICENSE_WITHDRAWN`：载荷还需包含 `effective_at`, `reason`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。

## 服务语义

`src/festival_exhibit/service.py` 中的 `LicenseService` 在契约之上落实以下规则，全部事件追加写入 JSONL 日志，重启后重放恢复：

- **授权授予与修订**（`grant_license`）：记录素材来源、权利人、用途渠道、期限与合作方。涉及活态传承的说明（`heritage_note`）须由权利人本人确认（`heritage_confirmed_by == right_holder`）；非权利人修订只能维持或收窄许可，不得增加渠道或延长期限。
- **使用登记**（`record_use`）：校验合作方、渠道、期限与撤回状态，记录节目版本、空间位置、宣传引用与责任人（`actor`）。
- **锁场冻结**（`freeze_event`）：冻结当晚实际使用的节目版本；同一实体展位在同一活动日只能被一个方案确认，并行预订的其余方案报 `position_conflict`。
- **授权撤回**（`withdraw_license`）：只影响生效时点之后尚未发生的使用（状态 `cancelled`）；已完成的展示保留证据（状态 `completed`）并按合作方生成后续说明义务（`removal_case`，kind 为 `explanation`）。
- **撤下回执**（`submit_removal_receipt`）：同一合作方相同回执编号重复提交不重复计数（`duplicate`）；编号相同但素材或范围不同进入争议（`disputed`）且不计数。
- **恢复与查询**：`recovery_report(now)` 汇总未完成撤展/说明义务、已到期或已撤回的授权与回执争议；`can_use(partner_id, asset_id, channel, at)` 按合作方回答素材当前能否用于指定渠道并给出依据；`explain_use(use_id)` 还原一次使用的授权依据（授权版本与范围快照）与责任人。
