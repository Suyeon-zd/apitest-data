# data 模块使用说明（批量测试数据构造工具）

> 本文档面向 `data/` 子系统。`data/` 与接口测试框架（`core/`、`testcases/`）**相互独立**：
> 不依赖 `core.*`，可整体拷贝到其他项目单独运行。

---

## 一、模块定位

`data/` 是一个**独立可拷贝**的批量造数工具，支持 4 种输出目标：

| target | 触发条件 | 输出 |
|---|---|---|
| `db`（默认） | 表定义有 `ddl` + `db` 字段 | 生成后 `INSERT` 到 MySQL / ClickHouse / 星环 |
| `api` | 表定义写 `target: api` 或含 `api` 字段 | 逐行 POST 到业务接口（走后端级联写入） |
| `csv` | 命令行带 `--csv 路径` | 流式分批导出 CSV，不连库 |
| `excel` | 表定义写 `target: excel` | 三阶段流水线：生成 xlsx → 上传导入 → 配对插 temp |

**依赖方向：**

```
data/  ←──（单向，可断）──  core/（仅 db_compare 断言借用 data/db/client.py）
```

---

## 二、目录结构

```
data/
├── __init__.py
├── cli.py                       # 命令行入口（argparse + 分发）
├── data_generator.md            # 本文档
│
├── infra/                       # 基础设施
│   ├── __init__.py
│   ├── errors.py                # DataError 异常体系
│   ├── env_loader.py            # .env + ${VAR} 展开 + 配置加载
│   ├── preview.py               # 预览格式化（竖版/横版/值分布）
│   └── auth_kit.py              # 登录能力唯一实现
│
├── db/                          # 数据库访问
│   ├── __init__.py
│   ├── client.py                # DBClient + DBManager（对外统一接口）
│   ├── base_driver.py           # 驱动抽象基类
│   └── drivers/                 # 具体驱动（注册表模式）
│       ├── __init__.py
│       ├── mysql.py
│       ├── clickhouse.py
│       └── transwarp.py
│
├── factory/                     # 造数引擎
│   ├── __init__.py
│   ├── ddl_parser.py            # 建表语句解析（MySQL / CK）
│   ├── value_generator.py       # 单字段值生成（Faker + 14 种规则）
│   └── factory.py               # 编排：DDL/fields + 规则 + 池 → 数据行
│
├── seeders/                     # 输出适配器
│   ├── __init__.py
│   ├── base.py                  # BaseSeeder 抽象基类
│   ├── http.py                  # target: api（继承 BaseSeeder）
│   └── excel/                   # Excel 三阶段
│       ├── __init__.py
│       ├── writer.py            # 阶段①：生成 xlsx
│       ├── importer.py          # 阶段②：上传导入 + manifest
│       └── temp_seeder.py       # 阶段③：回查配对插 temp
│
├── pipelines/                   # 编排层（各 target 的执行流水线）
│   ├── __init__.py              # 触发驱动注册
│   ├── base.py                  # BasePipeline + 注册表
│   ├── db_pipeline.py
│   ├── api_pipeline.py
│   ├── csv_pipeline.py
│   └── excel_pipeline.py
│
├── tables/                      # 表定义 YAML（一表一文件）
│   ├── asset_instock.yaml
│   ├── asset_app_system.yaml
│   ├── sys_dept.yaml
│   ├── sys_user.yaml
│   └── sys_user_api.yaml
│
└── templates/
    └── asset_daoru.xlsx         # Excel 官方导入母版
```

**数据流：**

```
       cli.py（argparse）
            │
            ▼
       pipelines/（决定 target）
            │
            ▼
       factory/（生成 rows）
            │
            ▼
       seeders/（写入目标）
            │
            ▼
       db/ | API | CSV | Excel
```

---

## 三、快速开始

### 3.1 安装依赖

```bash
pip install pyyaml faker requests openpyxl
pip install pymysql              # MySQL 用
pip install clickhouse-connect   # ClickHouse 用
pip install pyhive               # 星环（HiveServer2 协议）用
pip install python-dotenv        # .env 加载（可选）
```

### 3.2 查看规则

```bash
python -m data.cli --list-rules
```

### 3.3 生成脚手架

```bash
python -m data.cli --init my_table
# 生成 data/tables/my_table.yaml 模板
```

### 3.4 校验表定义

```bash
python -m data.cli --config data/tables/asset_instock.yaml --validate
```

### 3.5 预览

```bash
python -m data.cli --config data/tables/asset_instock.yaml --dry-run --preview 5
```

### 3.6 正式执行

```bash
# 写库
python -m data.cli --config data/tables/asset_instock.yaml

# 调接口
python -m data.cli --config data/tables/sys_user_api.yaml --count 100

# 导出 CSV
python -m data.cli --config data/tables/asset_instock.yaml --csv out.csv --count 100000

# Excel 三阶段
python -m data.cli --config data/tables/resource_ledger_temp.yaml --stage excel
```

---

## 四、命令行参数速查

| 参数 | 作用 | 默认 |
|---|---|---|
| `--config` | 表定义 YAML 路径（除 `--list-rules`/`--init` 外必填） | — |
| `--env` | 环境：dev / staging / prod | dev |
| `--db` | 覆盖表定义中的 db（数据库别名） | 表定义里的 db |
| `--count` | 覆盖生成行数 | 表定义里的 count |
| `--batch-size` | 覆盖批量大小 | 表定义里的 batch_size |
| `--seed` | 覆盖随机种子 | 表定义里的 seed |
| `--dry-run` | 只预览，不写库/不调接口 | — |
| `--csv 路径` | 导出 CSV（不连库） | — |
| `--preview N` | 预览前 N 行 | 3 |
| `--preview-format` | auto / vertical / table | auto |
| `--stage` | target=excel 的阶段：excel / upload / temp / all，支持逗号分隔 | all |
| `--rebuild-temp` | 阶段③ 先按批次前缀清空 temp 旧数据 | — |
| `--validate` | 只校验表定义，不生成数据 | — |
| `--list-rules` | 打印规则说明后退出 | — |
| `--init NAME` | 生成脚手架 `data/tables/NAME.yaml` | — |

---

## 五、表定义 YAML 结构

### 5.1 直插库（db）

```yaml
db: ck                         # 目标库别名（config/<env>.yaml → databases.<name>）
count: 100
batch_size: 1000
locale: zh_CN
# seed: 42                     # 可选：固定种子
# database: other_db           # 可选：写入到非默认库

ddl: |-                        # 建表语句内联（MySQL / CK 语法都支持）
  CREATE TABLE IF NOT EXISTS asset_instock
  (
      id            UInt64,
      asset_code    String,
      asset_name    String,
      status        LowCardinality(String),
      create_time   DateTime
  )
  ENGINE = MergeTree
  ORDER BY id;

rules:                         # 只写需要特殊处理的字段
  id: {sequence: {start: 1, step: 1}}
  asset_code: {template: "ZC-{random_int:8}"}
  status: {fixed: "在库"}
  create_time: {faker: date_time_this_year, monotonic: true}

unique: [asset_code]           # 全表唯一字段（撞车自动重试）

# pools:                       # 命名数据池（供 from_pool 消费）
#   pool_dept: {db: mysql, sql: "SELECT id FROM sys_dept", field: id}
#   pool_status: [0, 1, 2]     # 也支持内联列表
```

### 5.2 接口造数（api）

```yaml
target: api
name: sys_user
api:
  method: POST
  path: /system/user
count: 100
seed: 20260914
stop_on_error: false           # 单行失败继续/停止

fields:                        # 顺序即 JSON 键序
  - {name: idCard, type: string}
  - {name: deptId, type: int}
  - {name: roleIds, type: array}

rules:
  idCard: {id_card: {area: "341111", min_age: 18, max_age: 60}}
  deptId: {fixed: 100}
  roleIds: {fixed: []}

unique: [idCard]
```

### 5.3 Excel 三阶段

```yaml
target: excel
count: 1000000
rows_per_file: 10000           # 每个 Excel 数据行数
insert_batch_size: 50000       # 阶段③ temp 每批生成+插入行数
batch_prefix: "ZCBH260915-"    # 批次编号前缀（圈定本批数据）
output_dir: "output/asset_excel"
min_ledger_ratio: 0.95         # 阶段③ 允许的底账回查/预期行数下限

template:
  path: "data/templates/asset_daoru.xlsx"
  sheet: "应用系统"
  data_start_row: 3            # 数据起始行

excel_pools:                   # 母版选项 sheet → 规则数据池
  options_8: options_8
  options_30: options_30
  options_31: options_31
  options_35: options_35

unit_sql_pool:                 # SQL 池与模板下拉取交集
  db: mysql
  sql: "SELECT DISTINCT dept_name FROM sys_dept WHERE ..."
  field: dept_name
  intersect: [options_8, options_30, options_31]

import_api:
  path: "/asset/instock/v2/importInstockAsset"
  timeout: [5, 120]
  form: {importType: add, assetType: 应用系统, wrapFileLevel: 公开}

grouping:                      # IP 分组：同组字段跨行一致
  fields: [asset_code, sys_name, owner_unit, duty_dept, duty_person, ips]
  sizes: [1, 2, 3]
  weights: [85, 12, 3]

fields: [...]                  # 与母版列序一致
rules: {...}
unique: [asset_code, ips]

stage_temp:                    # 阶段③ 配置
  database: ODS
  table: resource_ledger_temp
  pair_keys: [...]
  diff_rules: {...}
  ddl: |-
    CREATE TABLE ODS.resource_ledger_temp (...)
    ENGINE = MergeTree
    ORDER BY resource_id
```

---

## 六、造数规则

### 6.1 取值规则（14 种）

| 规则 | 说明 | 示例 |
|---|---|---|
| `fixed` | 固定值 | `{fixed: "在库"}` |
| `choice` | 从列表随机选，支持 `weights` 权重 | `{choice: [A, B, C], weights: [1, 2, 3]}` |
| `range` | `[min, max]` 区间随机整数 | `{range: [1, 100]}` |
| `sequence` | 顺序自增 | `{sequence: {start: 1, step: 1}}` |
| `faker` | 调用任意 Faker 方法 | `{faker: name, faker_kwargs: {...}}` |
| `id_card` | 合法 18 位身份证 | `{id_card: {area: "341111", min_age: 18, max_age: 60}}` |
| `template` | 占位符模板 | `{template: "ZC-{random_int:8}"}` |
| `ref` | 引用本行其他字段 | `{ref: user_name, transform: pinyin}` |
| `switch` | 按本行另一字段条件映射 | `{switch: {"on": gender, cases: {...}}}` |
| `from_pool` | 从命名数据池随机抽 | `{from_pool: pool_dept_ids}` |
| `self_ref` | 引用本表已生成行的字段 | `{self_ref: {field: id, root: 0}}` |
| `ip_port` | `"IPv4:端口"` 格式 | `{ip_port: {port_min: 8000, port_max: 9000}}` |
| `null` | 固定生成 NULL | `{null: true}` |
| `skip` | 跳过该字段（交给数据库 DEFAULT） | `{skip: true}` |

### 6.2 修饰符（可与任意规则叠加）

| 修饰符 | 说明 | 示例 |
|---|---|---|
| `transform` | 值转换，可叠加 | `transform: [pinyin, upper]` |
| `monotonic` | 保证逐行递增（date/datetime/数值） | `monotonic: true` |

**transform 转换器：** `pinyin` / `upper` / `lower` / `md5` / `prefix:xxx` / `suffix:xxx`

### 6.3 template 占位符

| 占位符 | 说明 | 示例结果 |
|---|---|---|
| `{random_int}` | 6 位随机数 | `482193` |
| `{random_int:8}` | N 位随机数（补零） | `04821937` |
| `{seq}` | 顺序自增 | `1`、`2`、`3` |
| `{seq:6}` | 顺序自增（补零到 N 位） | `000001` |
| `{uuid}` | UUID 十六进制 | `a1b2c3...` |
| `{word}` | Faker 单词 | `hello` |
| `{name}` | Faker 姓名 | `张三` |
| `{phone}` | Faker 电话 | `13800138000` |
| `{email}` | Faker 邮箱 | `a@b.com` |

### 6.4 表级配置

| 配置 | 说明 |
|---|---|
| `seed` | 固定随机种子，同参数产出可复现 |
| `unique` | 全表唯一字段列表（撞车自动重试最多 50 次） |
| `pools` | 命名数据池（SQL 查询或内联列表） |
| `grouping` | 分组造数（同组字段跨行一致） |

### 6.5 未配置规则的字段

按 DDL 类型/约束自动生成：

- 整数按类型取值范围（含 `unsigned`）
- `decimal(P,S)` / Float 按精度
- `varchar(n)` 按长度
- `ENUM` 从枚举值中选
- 日期时间生成近一年的值
- `UUID` 生成 uuid4
- `Array(T)` 生成 1~3 个元素
- 列名含 `phone` / `email` / `id_card` / `url` / `address` 等自动选用对应 Faker
- MySQL `AUTO_INCREMENT` 字段自动跳过

---

## 七、Excel 三阶段流水线

### 7.1 分阶段执行（推荐逐个验证）

```bash
# 阶段①：生成 xlsx
python -m data.cli --config data/tables/resource_ledger_temp.yaml --stage excel

# 阶段②：上传导入
python -m data.cli --config data/tables/resource_ledger_temp.yaml --stage upload

# 阶段③：回查配对插 temp
python -m data.cli --config data/tables/resource_ledger_temp.yaml --stage temp
```

### 7.2 逗号分隔支持

```bash
python -m data.cli --config ... --stage excel,upload    # 只跑前两阶段
```

### 7.3 各阶段说明

| 阶段 | 动作 | 关键机制 |
|---|---|---|
| **excel** | 以官方母版分批生成 xlsx | 数据从第 3 行起，下拉数据校验区域自动扩展 |
| **upload** | 逐文件 multipart 上传导入 | `import_manifest.json` 断点续传；全部完成后一次总对账 |
| **temp** | 按批次前缀回查底账配对键 | 游标分页（`ips`+`resource_id`）；生成"合法但不同"的差异数据 |

### 7.4 失败重跑

`upload` 阶段的进度文件 `output/asset_excel/import_manifest.json` 记录每个文件的状态，重跑时自动跳过 `status=success` 的文件。

### 7.5 回滚 temp 旧数据

```bash
python -m data.cli --config data/tables/resource_ledger_temp.yaml --stage temp --rebuild-temp
```

CK 的 `DELETE` 是异步 mutation，稍等后回查。

---

## 八、数据库驱动

### 8.1 支持的数据库

| type | 驱动包 | 别名 |
|---|---|---|
| `mysql` | pymysql | — |
| `clickhouse` | clickhouse-connect | `ck` |
| `transwarp` | pyhive / quark | `inceptor` / `argodb` / `stellardb` |

### 8.2 配置示例

在 `config/<env>.yaml` 的 `databases` 下配置：

```yaml
databases:
  mysql:
    type: mysql
    host: "10.148.242.205"
    port: 3306
    user: "root"
    password: "${MYSQL_PASSWORD}"
    database: "soc-cloud"
    charset: "utf8mb4"
  ck:
    type: clickhouse
    host: "10.148.242.206"
    port: 8123
    username: "issa"
    password: "${CK_PASSWORD}"
    database: "ODS"
  transwarp:
    type: transwarp
    host: "10.148.242.xxx"
    port: 10000
    auth_mechanism: "LDAP"
    user: "${TW_USER}"
    password: "${TW_PASSWORD}"
    database: "ODS"
```

### 8.3 新增数据库驱动

只需 3 步：

1. 在 `data/db/drivers/` 下新建 `xxx.py`
2. 继承 `BaseDBDriver` 并实现 `connect` / `query` / `insert_many`
3. 用 `@register("type别名")` 装饰

**示例：**

```python
# data/db/drivers/postgresql.py
import itertools
import logging

from data.db.base_driver import BaseDBDriver
from data.db.drivers import register

logger = logging.getLogger(__name__)


@register("postgresql", "pg")
class PostgreSQLDriver(BaseDBDriver):

    def connect(self) -> bool:
        try:
            import psycopg2
        except ImportError:
            logger.warning(f"[{self.name}] psycopg2 未安装")
            return False
        try:
            self._conn = psycopg2.connect(
                host=self.config.get("host"),
                port=self.config.get("port", 5432),
                user=self.config.get("user"),
                password=self.config.get("password"),
                dbname=self.config.get("database"),
            )
            self._current_db = self.config.get("database")
            return True
        except Exception as e:
            logger.error(f"[{self.name}] PostgreSQL 连接失败: {e}")
            return False

    def query(self, sql, database=None):
        if not self._conn:
            return []
        with self._conn.cursor() as cur:
            cur.execute(sql)
            columns = [d[0] for d in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def insert_many(self, table, columns, rows, batch_size=1000, database=None):
        if not self._conn:
            return 0
        col_sql = ", ".join(f'"{c}"' for c in columns)
        placeholders = ", ".join(["%s"] * len(columns))
        sql = f'INSERT INTO "{table}" ({col_sql}) VALUES ({placeholders})'
        total = 0
        with self._conn.cursor() as cur:
            iterator = iter(rows)
            while True:
                batch = list(itertools.islice(iterator, batch_size))
                if not batch:
                    break
                cur.executemany(sql, [tuple(r) for r in batch])
                self._conn.commit()
                total += len(batch)
        return total
```

最后在 `data/db/drivers/__init__.py` 底部加一行 import 触发注册：

```python
from data.db.drivers import postgresql   # noqa: F401, E402
```

**不需要改动 `client.py`。**

---

## 九、环境变量与密钥管理

YAML 中支持 `${VAR}` 与 `${VAR:-default}` 占位符，加载时由 `data/infra/env_loader.py` 替换。

**优先级（从高到低）：**

1. 系统环境变量
2. 项目根目录 `.env` 文件
3. 占位符里的默认值

**示例：**

```yaml
password: "${MYSQL_PASSWORD}"          # 必填，未设置时为空串 + 告警
password: "${MYSQL_PASSWORD:-123456}"  # 未设置时用默认值
```

**本地开发：** 复制 `.env.example` 为 `.env`，填入真实值（`.env` 不入库）

**CI：** 直接在平台配置同名环境变量（Jenkins/GitLab/GitHub Secrets）

---

## 十、常见问题

**Q1：运行报 `pymysql 未安装` / `clickhouse-connect 未安装`？**

```bash
pip install pymysql clickhouse-connect
```

**Q2：`--validate` 报 `规则字段 xxx 不在字段列表中`？**

检查表定义中 `rules` 的 key 是否都在 DDL / fields 中出现。运行时若不启用 `--validate`，仅打印 warning 后忽略。

**Q3：`switch.on` 行为异常？**

YAML 1.1 会把 `on` 解析成布尔 `True`。**建议在 YAML 中写 `"on": field_name`（带引号）**。代码已能自动兼容并告警。

**Q4：百万行造数内存爆？**

- **CSV 导出**：已流式分批，不会 OOM
- **直插库**：`insert_many` 已支持 `Iterable`，但 `db_pipeline` 当前仍是全量生成后一次插入。如需彻底流式，可改造 pipeline 分批调用 `factory.build_rows`

**Q5：`--stage` 支持哪些值？**

`excel` / `upload` / `temp` / `all`，支持逗号分隔组合（如 `--stage excel,upload`）。

**Q6：如何让两次造数结果完全一致？**

加 `seed`：表定义里写 `seed: 42`，或命令行 `--seed 42`。

**Q7：数据池报 `需要 DB 查询，但未提供数据库连接`？**

表定义的 `pools` 中有 SQL 池但连不上库。检查 `config/<env>.yaml` 的 `databases` 配置与网络连通性；若只想离线生成，把 SQL 池改为内联列表。

**Q8：接口造数怎么判定成功？**

按响应 JSON `code == 200` 判定（RuoYi 风格）。单行失败会记录到 `failures` 列表，最后汇总；`stop_on_error: true` 时首条失败即停止。

**Q9：如何回退 Excel 阶段产生的大量数据？**

- Excel 产物：删除 `output/asset_excel/` 目录
- temp 表数据：`--stage temp --rebuild-temp` 会按 `batch_prefix` 清空后重建
- 底账（resource_ledger）：需要按 `batch_prefix` 手动清理（当前未提供一键回滚）

**Q10：`--preview-format` 怎么选？**

- `auto`（默认）：列数 > 8 自动竖版，否则横版
- `vertical`：字段名在左，每行值横向排列（宽表友好）
- `table`：每行值一列对齐（窄表友好）

---

## 十一、扩展指南

### 11.1 新增造数规则

在 `data/factory/value_generator.py` 的 `_by_rule` 方法中加一个分支：

```python
if "my_rule" in rule:
    return "自定义逻辑"
```

YAML 中即可使用 `{my_rule: ...}`。

### 11.2 新增 Pipeline（新 target 类型）

1. 在 `data/pipelines/` 下新建 `my_pipeline.py`
2. 继承 `BasePipeline`，实现 `run()`
3. 用 `@register("my_target")` 装饰
4. 在 `data/pipelines/__init__.py` 底部 import 触发注册

无需改 `cli.py`。

### 11.3 新增数据库驱动

见 8.3 节。

### 11.4 新增 Seeder

在 `data/seeders/` 下新建模块，继承 `BaseSeeder`，实现 `login` 和 `_write_one`。

---

## 十二、设计与约定

### 12.1 独立性

- `data/` 不 import `core.*`
- `data/` 只用标准库 + `faker` / `requests` / `openpyxl` / `pyyaml`
- 可整体拷贝到其他项目

### 12.2 异常体系

所有异常继承 `data/infra/errors.py` 的 `DataError`：

```
DataError（code=1）
├── ConfigError        配置错误
├── ValidationError    YAML 校验失败
├── ConnectionError    数据库连接失败
├── GenerationError    数据生成错误
├── LoginError         登录失败
└── PartialFailure     部分失败（code=2，便于 CI 感知）
```

`cli.py` 的 `main()` 顶层捕获 `DataError`，按 `code` 决定退出码。

### 12.3 日志

- `data/` 内的模块统一用 `logging.getLogger(__name__)`
- 独立运行（`python -m data.cli`）时由 `cli.py` 的 `_setup_logging` 配置输出
- 在 pytest 下运行时由 `core/logger.py` 配置 root logger

### 12.4 敏感信息

- YAML 中密码等敏感项写 `${VAR}` 占位符
- 本地用 `.env`，CI 用平台环境变量
- 不提交明文

---

## 十三、常用命令速查

```bash
# 帮助与规则
python -m data.cli --list-rules
python -m data.cli --init my_table

# 校验
python -m data.cli --config data/tables/xxx.yaml --validate

# 预览
python -m data.cli --config data/tables/xxx.yaml --dry-run
python -m data.cli --config data/tables/xxx.yaml --dry-run --preview 10

# 写库
python -m data.cli --config data/tables/xxx.yaml
python -m data.cli --config data/tables/xxx.yaml --count 5000 --batch-size 2000
python -m data.cli --config data/tables/xxx.yaml --db ck --env staging

# 接口造数
python -m data.cli --config data/tables/xxx.yaml --count 100

# CSV 导出
python -m data.cli --config data/tables/xxx.yaml --csv out/xxx.csv --count 100000

# 固定种子（可复现）
python -m data.cli --config data/tables/xxx.yaml --csv out.csv --seed 42

# Excel 三阶段
python -m data.cli --config data/tables/resource_ledger_temp.yaml --stage excel
python -m data.cli --config data/tables/resource_ledger_temp.yaml --stage upload
python -m data.cli --config data/tables/resource_ledger_temp.yaml --stage temp
python -m data.cli --config data/tables/resource_ledger_temp.yaml --stage temp --rebuild-temp

# Excel 分阶段合并
python -m data.cli --config data/tables/resource_ledger_temp.yaml --stage excel,upload
```

---

## 十四、后续可优化项

- `data/tests/` 单元测试（`ddl_parser` / `value_generator` / `factory` 均为纯函数）
- `db_pipeline` 分批生成（当前仍全量驻留内存）
- 造数批次台账（每次执行落结构化 JSON）
- `--cleanup` 按 `batch_prefix` 一键回滚
- 并发/分片造数（按行号区间多进程）
- 跨字段业务约束（如 `end_time > start_time`）
- Allure / HTML 报告集成（若需要）

---

**文档最后更新：** 2026-09-24