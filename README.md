# QQ 邮箱发票归集

一个在 Windows 本地运行的 QQ 邮箱发票整理工具。按收件日期扫描邮件，提取金额明确的 PDF 和图片发票；开票二维码、网页下载页与待核对内容分开放，处理结果留有清单。支持多个 QQ 邮箱，授权码按邮箱保存在当前 Windows 用户的凭据管理器中。

> **先核对，再开启自动删除。** 程序首次运行默认保留原邮件。只有你勾选自动删除，且整封邮件的发票都已确认处理完毕时，才会将原邮件移入“已删除”。

## 下载与启动

**[下载最新 Release](https://github.com/neal240/qq-invoice-collector/releases/latest)** 的 `qq-invoice-collector-win-x64.zip` → 解压 → 双击 `启动发票归集.cmd`。

适用 **64 位 Windows 10/11**。Release ZIP 已带 PDF 读取库和 [Python 官方安装程序](https://www.python.org/downloads/release/python-31315/)；电脑没有可用的 Python 时，首次启动会显示安装提示，安装结束后自动打开界面。无需手动运行 `pip install`。建议将 ZIP 解压到自己有写入权限的文件夹。

### 获取 QQ 邮箱授权码

在 QQ 邮箱网页版依次进入 **设置 → 账号与安全 → 安全设置 → 生成授权码/授权码管理**，开启 IMAP 并获取授权码。这里填的是 **授权码，不是 QQ 登录密码**；“SMTP 发信后保存到服务器”无需勾选。

打开软件后，输入邮箱和授权码，选择收件日期范围与保存目录，点击“开始归集”。登录成功后授权码会保存在本机；下次从邮箱下拉框选择同一个账号即可。更换邮箱时输入另一邮箱及其授权码。日期范围包含开始和结束当天。

## 处理规则

| 邮件内容 | 程序处理 | 勾选自动删除后的原邮件 |
| --- | --- | --- |
| 金额 **大于 5.00 元** 的 PDF 发票 | 保存 PDF 到 `发票_大于5元` | 确认成功保存且整封邮件无未处理项目后移入“已删除” |
| 金额 **小于 5.00 元** 的发票 | 不保存发票文件 | 金额明确且整封邮件处理完成后移入“已删除” |
| 金额 **恰好 5.00 元** | 不归档 | 保留 |
| PNG/JPG 发票 | 使用 Windows 本机中文 OCR 识别；金额及发票信息明确才归档 | 判断规则同 PDF；识别不清则保留 |
| PDF、XML、OFD 同时附在一封邮件 | 只处理 PDF，忽略 XML/OFD | PDF 已确认处理完成时可删除 |
| 只有 XML/OFD | 不下载，也不列为待核对 | 保留 |
| ZIP 附件或直链 | 仅提取其中的 PDF/图片；直链尝试下载 | 必须确认下载内容已处理完成 |
| 开票二维码、需在网页点击下载的发票 | 保存二维码和邮件信息，或列入 `网页下载_待处理` | 保留；若同封邮件已有匹配且处理完成的 PDF，可删除 |

无法确定金额的 PDF 或图片进入 `待核对`。网页下载页会附带 `打开链接.html`；手动从网页下载 PDF/图片后，可在软件中点击“导入手动下载的 PDF/图片”。**手动导入不会自动删除原邮件。** 图片型 PDF 暂不进行 OCR；网页链接若过期、需要登录或下载失败，也需要手动处理。

结果按 `归档结果 / 邮箱地址 / 起止日期` 分开保存，并生成 `处理清单.csv` 与 `删除状态.csv`。重复运行会跳过已保存的相同附件。自动删除只是移入 QQ 邮箱的“已删除”，程序不会清空该文件夹。

## 隐私与数据

- 发票和 OCR 处理在本机进行；图片不会上传到识别服务。
- 授权码保存在 **Windows 凭据管理器**，不写进 `settings.json`、处理清单或日志。`settings.json` 只记住上次选择的邮箱、日期、保存位置和删除选项。
- 分享用的 Release ZIP 按白名单制作，不含个人邮箱设置、授权码、发票或处理记录。请勿把自己的整个工作目录直接压缩公开。
- 如果 Windows 没有中文 OCR 语言包，或识别不出完整发票及金额，相关图片会留在 `待核对`，原邮件保留。

## 从源码运行与打包

克隆仓库后，在 Windows 上双击 `启动发票归集.cmd`。若缺少 Python，源码目录没有内置安装程序时，请从 [Python 官方页面](https://www.python.org/downloads/windows/)安装带 Tcl/Tk 的 64 位 Python 3.10 或更新版本；随后重新双击启动脚本。也可以在终端运行：

```powershell
python invoice_mail.py
```

命令行支持 `--email`、`--start YYYY-MM-DD`、`--end YYYY-MM-DD`、`--output-root`，以及本次保留邮件的 `--keep-mail`。运行测试：

```powershell
python -m unittest -q test_invoice_mail
```

制作带首次安装程序的发行 ZIP：先从 [Python 3.13.15 发布页](https://www.python.org/downloads/release/python-31315/)下载 64 位 Windows 安装程序，保存为 `发行资源/python-3.13.15-amd64.exe`，然后运行 `powershell -ExecutionPolicy Bypass -File .\制作分享包.ps1`。脚本只打包白名单中的源码、说明、许可证、PDF 库和安装程序。

## 许可证

本项目采用 [MIT License](LICENSE)。随项目附带的 `pypdf` 保留其原始 [许可证](vendor/LICENSE-pypdf.txt)；发行 ZIP 中的 Python 安装程序版权归 Python Software Foundation 所有。
