# GitHub 上传步骤（Windows）

## 只上传发布副本

在整理后的 `outputs/github/contract-ledger` 文件夹打开 PowerShell。不要在原业务项目根目录执行 `git add .`，也不要上传整个业务目录或压缩包。

这个副本只包含代码、测试、英文说明和虚构数据生成脚本。真实合同、运行数据库、密钥、导出资料和公司专用脚本没有复制进来。公开前仍需确认你有权公开这份业务代码；文档没有擅自授予开源许可。

## 1. 准备账号

登录 GitHub，在 https://github.com/new 创建空仓库。

- Repository name：`contract-ledger`
- Description：`Local-first contract archiving and financial reconciliation with Python, Flask, SQLite and human-reviewed OCR.`
- 可先选 Private 做最后检查；需要招生老师直接访问时，再改 Public。
- 不勾选初始化 README、.gitignore 或 License，因为本地已经准备了对应文件或说明。

## 2. 本地运行一次

按 README 创建 Python 3.12 虚拟环境并安装依赖。执行 `python -m pytest -q` 时使用该虚拟环境的解释器。需要演示时先执行 `scripts/demo.py`，再启动服务。

## 3. 初始化并检查文件

以下命令必须在发布副本内执行。整理时已初始化本地 Git；再次执行 `git init -b main` 通常只会提示已存在仓库。

```powershell
git status
git add .
git diff --cached --stat
git diff --cached --name-only
```

预期包含 `contractdb/`、`tests/`、`scripts/demo.py`、`docs/`、README、依赖清单和 `.github/workflows/tests.yml`。不得出现 `data/`、真实合同、数据库、密钥、备份或导出文件。

首次提交前设置自己的署名，只针对这个仓库设置即可：

```powershell
git config user.name "你的 GitHub 用户名"
git config user.email "你的 GitHub 提交邮箱"
git commit -m "Prepare contract ledger portfolio edition"
```

邮箱可使用 GitHub Settings > Emails 中显示的个人 noreply 地址，不要照抄其他人的地址。项目未伪造旧提交历史，也未预设你的身份。

## 4. 连接远程并推送

把下一条命令中的 `YOUR_USERNAME` 换成你的 GitHub 用户名：

```powershell
git remote add origin https://github.com/YOUR_USERNAME/contract-ledger.git
git push -u origin main
```

Git for Windows 通常会通过凭据管理器引导浏览器登录；按实际提示完成。不要把 GitHub 密码或访问令牌写入项目文件。若提示远程已存在，先用 `git remote -v` 检查地址，不要重复添加。

## 5. 检查网页结果

确认首页正常显示英文 README、代码目录齐全。在 Actions 页面查看 Tests 是否通过；本地通过不代表云端已经通过。需要公开展示时，在 Settings > General > Danger Zone > Change repository visibility 改为 Public，并在未登录的浏览器窗口测试链接。

仓库 About 可填上面的英文描述和 python、flask、sqlite、ocr、document-management 等主题。不要把 GitHub Pages 当成这个后端应用的在线服务器。

## 6. 加到申请材料

CV 的 Projects 栏放仓库链接，并按 `APPLICATION_GUIDE_ZH.md` 如实描述本人贡献和 AI 辅助方式。推荐阅读顺序：README → CASE_STUDY → ENGINEERING → DEMO。后续改进用真实提交记录，不补造开发历史。

GitHub 官方参考：

- https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github
- https://docs.github.com/en/desktop/adding-and-cloning-repositories/adding-an-existing-project-to-github-using-github-desktop

这些步骤整理于 2026-09-28，界面文字可能随 GitHub 更新而变化。
