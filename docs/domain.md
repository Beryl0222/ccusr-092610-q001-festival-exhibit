# 领域约定

香港红磡举办第二届华服嘉年华，活动把自贡花灯技艺、航天展品、中秋文化、英歌舞与人形机器人、学生粤语朗诵和无人机灯光秀汇聚在同一场域。

聚合对象包括 `asset_license`、`event_release`、`partner_use`、`removal_case`。事件类型包括 `LICENSE_GRANTED`、`USE_RECORDED`、`EVENT_FROZEN`、`LICENSE_WITHDRAWN`、`REMOVAL_CONFIRMED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `LICENSE_GRANTED`：载荷还需包含 `right_holder`, `scope`。服务层在载荷中同时记录 `grant_id`、`asset`（素材来源与活态传承属性）、`partner_id`、`holder_confirmed` 与 `granted_by`。
- `EVENT_FROZEN`：载荷还需包含 `release_version`, `frozen_at`。服务层另记录 `night`（场次夜晚）与 `snapshot`（当晚实际使用版本快照）。
- `LICENSE_WITHDRAWN`：载荷还需包含 `effective_at`, `reason`。撤回按素材聚合，生效时点之后的使用才受影响。
- `USE_RECORDED`：服务层记录素材、合作方、渠道、节目版本、空间位置、宣传引用、使用时间、归属夜晚与登记责任人。
- `REMOVAL_CONFIRMED`：服务层记录撤展案件、回执编号、素材、范围与确认责任人。

## 服务层规则

`src/festival_exhibit/service.py` 在基础事实之上实现履约规则：

1. **角色边界**：权利人（`right_holder`）、商务（`business`）、运营（`operations`）。活态传承素材的许可确认与范围扩大只能由权利人本人作出，商务不能代替扩权，也不能发起撤回。
2. **锁场**：`EVENT_FROZEN` 冻结该夜晚实际使用的版本快照；冻结后该夜晚不再接受使用登记、展位确认或重复冻结，临时换节目只能在锁场前发生。
3. **撤回边界**：撤回自 `effective_at` 起生效；生效之后的使用生成待撤展案件，生效之前已完成的展示保留证据并生成后续说明义务，不因撤回而删除。
4. **换节目**：锁场前同一夜晚、同一位置、同一渠道出现不同节目版本时，被替换的露出承诺生成孤儿说明义务。
5. **回执幂等与争议**：相同回执编号、相同素材与范围的提交只计一次；编号相同但素材或范围不同，连同 `event_id` 相同但业务关键内容不同的事件，一律写入争议簿而不进日志。
6. **展位唯一**：同一实体展位同一夜晚只能被一个方案确认，同方案重试幂等，并行方案互斥。
7. **重启恢复**：状态全部由事件日志重放重建。`pending_matters` 汇总未完成撤展、说明义务（含孤儿露出承诺）与已到期授权；`can_use` 按合作方回答某素材在指定渠道与时点是否可用；`use_basis` 还原一次使用的授权依据、权利人、登记责任人、锁场快照、撤案与回执链。

存储为只追加 JSONL（`events.jsonl`），争议持久化在旁路文件（`events.jsonl.disputes.json`）。
