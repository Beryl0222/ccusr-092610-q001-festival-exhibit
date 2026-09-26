# 节庆展陈授权履约簿

香港红磡举办第二届华服嘉年华，活动把自贡花灯技艺、航天展品、中秋文化、英歌舞与人形机器人、学生粤语朗诵和无人机灯光秀汇聚在同一场域。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/festival_exhibit/`：基础契约校验、事件存储与授权履约服务。
  - `contracts.py`：事件信封契约校验，不改写调用方输入。
  - `store.py`：只追加 JSONL 存储，版本递增、编号幂等与争议隔离。
  - `model.py`：角色、范围、时间与查询结果模型。
  - `errors.py`：业务规则拒绝类型。
  - `service.py`：授权、使用、锁场、撤回、撤展回执与恢复查询。
- `tests/`：信封契约与服务业务规则测试。
- `docs/domain.md`：领域对象、事件语义与服务层规则。

## 服务用法

```python
from datetime import datetime, timedelta, timezone
from festival_exhibit import Actor, LicensingService

svc = LicensingService.open("./data/ledger")
holder = Actor("holder-zigong", "right_holder")
biz = Actor("biz-hung-hom", "business")
ops = Actor("ops-stage", "operations")
now = datetime.now(timezone.utc)

# 登记素材来源并授权：活态传承素材的扩权须权利人本人确认
svc.grant_license(
    asset_id="A-lantern", title="缠枝莲纹自贡花灯",
    origin="自贡灯会传承人口授图样", right_holder="holder-zigong",
    partner_id="P-troupe", channels=("stage", "promotion"),
    valid_from=now - timedelta(days=1), valid_to=now + timedelta(days=30),
    actor=holder, living_heritage=True,
    heritage_note="传统缠枝莲纹由传承人按祖制扎制", holder_confirmed=True,
)

# 按合作方回答：某素材当前能否用于指定渠道
svc.can_use("A-lantern", "P-troupe", "stage", as_of=now)  # allowed=True

# 登记当晚使用（节目版本、空间位置、宣传引用随用记录）
use = svc.record_use(
    asset_id="A-lantern", partner_id="P-troupe", channel="stage",
    use_time=now, actor=ops, night="2026-09-26",
    program_version="v1", space="主舞台",
    heritage_note="传承人确认的纹样说明",
)

# 锁场：冻结当晚实际使用版本
svc.freeze_release(night="2026-09-26", release_version="v1", actor=ops)

# 撤回：之后的使用生成撤展案件，之前已完成的展示保留证据并生成说明义务
svc.withdraw_license(asset_id="A-lantern", effective_at=now + timedelta(days=2),
                     reason="传承人要求撤回", actor=holder)
svc.confirm_removal(receipt_no="R-001", case_id="RC-U-0002", confirmed_by=ops)

# 重启后仍可找回未完成事项，并还原一次使用的依据与责任人
LicensingService.open("./data/ledger").pending_matters()
svc.use_basis(use["use_id"])
```

业务规则拒绝时抛出 `festival_exhibit.errors` 下的类型（如 `PermissionDenied`、
`ReleaseFrozen`、`BoothAlreadyConfirmed`、`ReceiptConflict`）；编号相同但素材或范围
不同的提交进入争议簿，可通过 `svc.disputes()` 查看。

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
