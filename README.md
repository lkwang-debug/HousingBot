# HousingBot

## 从GitHub获取项目

仓库只包含代码、依赖、操作说明、场景模板和离线测试所需的公开通告样本。真实`.env`、推送群编码、数据库、资格名单、签名查询私有数据包、日志、截图及发布成品均不上传。

```powershell
Copy-Item .env.example .env
Copy-Item data/groups.example.json data/groups.json
```

已有本地配置不要覆盖。填写自己的推送凭证和群编码后再部署；示例群默认禁用。迁移正在运行的实例时，数据库及本地群配置须通过私有备份单独迁移，不能只依赖GitHub源码，否则可能丢失去重和投递状态。

本地验证只使用测试数据库及模拟推送，不运行`scan --deliver`、计划任务安装或云端发布。测试命令见`requirements-dev.txt`对应依赖和`python -X utf8 -m pytest tests`。

分类推送：四类保租房交叉统计、公租房独立统计，正文按市级和各区展开。原20:30任务已升级为固定晚报，操作说明见 [分类推送与固定晚报](docs/分类推送与固定晚报.md)。

住房业务 v2：支持多个全市群独立去重和补发。迁移、人工核对、查询页上传及回滚步骤见 [多群运行与修复说明](docs/多群运行与修复说明.md)。首次升级先运行 `python -m app.housing.cli migrate` 演练，再运行 `migrate --apply`。

深圳保障房（保租房 / 公租房）通告监控，推送到微信。商业化操作手册见 [docs/商业化方案.md](docs/商业化方案.md)。

## 目录

```
app\            代码（只有 housing 这条线，不含股票逻辑）
data\stockbot.db  唯一状态文件：通告 + 推送记录
data\groups.json  区域/档位 → PushPlus 群组编码
logs\           运行日志
scripts\        setup / 计划任务 安装卸载
.env            推送 token 与配置
.venv\          Python 虚拟环境（每台机器重建，不要拷）
```

## 首次部署

```powershell
# 前提：已装 Python 3.10 64 位（勾选 Add to PATH）
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
# 按提示填 .env 里的 PUSHPLUS_TOKEN，再跑一次 setup.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\install_housing_tasks.ps1
```

## 每次换电脑

1. 拷整个 `HousingBot` 目录，**删掉 `.venv`**；
2. 跑 `scripts\setup.ps1`（重建虚拟环境 + 建表）；
3. 跑 `scripts\install_housing_tasks.ps1`（计划任务不跟着电脑走）；
4. `data\stockbot.db` 一定要带过去，否则会重复推最近三天的通告。

## 日常命令

```powershell
.\.venv\Scripts\python.exe -m app.housing.cli groups          # 哪些群生效
.\.venv\Scripts\python.exe -m app.housing.cli scan --dry-run  # 看抓到了什么
.\.venv\Scripts\python.exe -m app.housing.cli scan --deliver  # 抓 + 推
.\.venv\Scripts\python.exe -m app.housing.cli digest --dry-run
.\.venv\Scripts\python.exe -m app.housing.cli remind --dry-run # 管家版提醒（认租截止）
.\.venv\Scripts\python.exe -m app.housing.cli list --open      # 正在认租的批次
.\.venv\Scripts\python.exe -m app.housing.cli batches --days 120 # 批次五步走到哪
.\.venv\Scripts\python.exe -m app.housing.cli sources --verify # 数据源体检

# 本地名单构建（签名模式下完整数据写入 data/lookup-source，不公开上传）
.\.venv\Scripts\python.exe -m tools.build_lottery_lookup --district 光明 --out lookup/guangming.html
.\.venv\Scripts\python.exe -m tools.build_lottery_lookup --district 宝安 --out lookup/baoan.html
.\.venv\Scripts\python.exe -m tools.build_lottery_lookup --index
```

查询入口已改为服务端签名、北京时间当天有效。名单变化后须同时更新云函数私有数据包和公开页面，不能只上传旧版 HTML。构建、线上核对及发布步骤见 [当日签名查询部署说明](docs/查询链接当天有效与云端发布.md)。`lookup/` 仅保存不含完整名单的公开页面；不要上传 `data/lookup-source/` 或 `.env`。
