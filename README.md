# 节庆展陈授权履约簿

香港红磡举办第二届华服嘉年华，活动把自贡花灯技艺、航天展品、中秋文化、英歌舞与人形机器人、学生粤语朗诵和无人机灯光秀汇聚在同一场域。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/festival_exhibit/contracts.py`：基础契约校验。
- `src/festival_exhibit/service.py`：展陈授权履约服务（授权、使用、冻结、撤回、回执、恢复查询）。
- `src/festival_exhibit/cli.py`：命令行校验入口。
- `tests/`：信封、时间、版本、事件载荷与服务规则测试。
- `docs/domain.md`：领域对象、事件语义与服务规则。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m festival_exhibit.cli contracts/domain.schema.json data/sample.json
```

样例有效时输出 `valid`；发现问题时逐行给出字段、代码和中文说明，并返回非零状态。

## 服务用法

```python
from festival_exhibit.service import LicenseService

service = LicenseService("data/journal.jsonl")  # 事件追加写入日志，重启后重放恢复
service.grant_license(
    event_id="evt-001", occurred_at="2026-09-01T09:00:00+08:00",
    license_id="lic-1", asset_id="asset-pattern", source="自贡灯彩纹样库",
    right_holder="holder-1", channels=["stage", "promotion"],
    valid_from="2026-09-01T00:00:00+08:00", valid_to="2026-10-31T23:59:59+08:00",
    actor="holder-1", partners=["partner-stage"],
)
service.can_use(partner_id="partner-stage", asset_id="asset-pattern",
                channel="stage", at="2026-09-25T20:00:00+08:00")
service.recovery_report("2026-11-01T00:00:00+08:00")
```
