# 领域约定

香港红磡举办第二届华服嘉年华，活动把自贡花灯技艺、航天展品、中秋文化、英歌舞与人形机器人、学生粤语朗诵和无人机灯光秀汇聚在同一场域。

聚合对象包括`asset_license`、`event_release`、`partner_use`、`removal_case`。事件类型包括`LICENSE_GRANTED`、`USE_RECORDED`、`EVENT_FROZEN`、`LICENSE_WITHDRAWN`、`REMOVAL_CONFIRMED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `LICENSE_GRANTED`：载荷还需包含 `right_holder`, `scope`。
- `EVENT_FROZEN`：载荷还需包含 `release_version`, `frozen_at`。
- `LICENSE_WITHDRAWN`：载荷还需包含 `effective_at`, `reason`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。
