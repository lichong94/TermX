# TermX 开源发布记录（2026-11-23）

本轮把 TermX v1.4.20（缺陷修复版）上传到 GitHub 与 Gitee，两个仓库均为**公开**，源码、文档与两个安装包全部在线可下载。

## 仓库地址

| 平台 | 地址 | 可见性 | 默认分支 |
|---|---|---|---|
| GitHub | https://github.com/lichong94/TermX | 公开 | main |
| Gitee | https://gitee.com/lichong2/termx | 公开（开源） | main |

两处远端 HEAD 完全一致：`b609b1252bbe59364d04df496b87f29dd686808f`。

## 提交结构（三个提交，两平台相同）

| 提交 | 内容 | 体积 |
|---|---|---|
| `4b026c6` | 源码（`工程/src/termx` 15 个模块）、随包使用说明、功能测试报告、v1.4.20 缺陷修复记录、README、图标 | 3.3 MB |
| `530e752` | 绿色免安装包 `工程/dist/TermX_绿色免安装包.zip` | 61.01 MB |
| `b609b12` | 单文件版 `工程/dist/TermX_单文件版.exe` | 69.13 MB |

拆成三次提交的原因：单次推送 131 MB 会触发 GitHub HTTP 408 超时，分批推送可稳定完成。GitHub 对 >50 MB 单文件给出 LFS 建议性警告，不影响下载。

## 安装包校验值

```
SHA256  71997e4f89069159246443993f02cfd389eaf7ff233aa3a6df1374115d0975dd
        TermX_单文件版.exe        72,488,577 字节

SHA256  ff0bd8910ed51f6ee165beb051f0c5280a73eaf33bb95ba7fc6e6d894ec4ba48
        TermX_绿色免安装包.zip     63,971,364 字节
```

公开下载实测（匿名、无凭据）：

- GitHub `raw.githubusercontent.com/lichong94/TermX/main/...` → HTTP 200，字节数与上表一致
- Gitee 仓库树匿名可读，两个安装包均在 `工程/dist/` 下

## 未上传的内容及原因

公开仓库只放面向使用者的内容，以下保留在本地 `D:\项目管理\TermX`，全部写入 `.gitignore`：

| 目录 / 文件 | 原因 |
|---|---|
| `素材/` | WorkBuddy 历史会话原文，含真实云账号 AccessKey（曾被 GitHub 推送保护拦截） |
| `文档/TermX-项目交接文档.md`、`文档/接手核验-2026-11-18.md` | 内部交接文档，含服务器地址、部署路径、口令等运维信息 |
| `文档/README-项目内部版.md` | 项目内部版说明（含本机路径与发布 SOP） |
| `发布记录/` | 各版本发布记录，含服务器归档路径 |
| `工程/docs/云服务器部署说明.txt`、`手机宝塔部署步骤.txt`、`官网实时同步说明.txt` | 内部部署文档 |
| `工程/dist/TermX_源码.zip` | 压缩包里含内部文档副本 |
| `工程/src/TermX_Data/` | 自测生成的示例会话库（数百条假主机），运行数据不入库 |
| `工具/`、`工程/vendor/`、`工程/build/`、`.deploy_key/`、`.release_keys/` | 运行时、第三方依赖、部署密钥，体积达 838 MB 级且不适合公开 |

## 敏感性处置经过

首次推送时把内部文档一并推上了 GitHub，公开仓库因此短暂暴露了服务器 IP、MySQL root 口令与部署密钥路径。处置动作：

1. 本地用 `.gitignore` 排除全部内部文档，重建为干净的三提交历史（当前 `b609b12` 系列）。
2. 旧仓库（含旧提交对象、可被按 SHA 直接访问）已通过 GitHub API 改名 `TermX-archive` 并转为**私有**，匿名访问实测 404，外泄面归零。
3. 同名公开仓库 `lichong94/TermX` 重新创建，干净历史重新推送。

> 归档仓库 `lichong94/TermX-archive` 仅本人可见，内含旧内部文档。如需彻底删除，用具备 `delete_repo` 权限的令牌或网页 Danger Zone 删除（当前推送令牌只含 `repo` 权限，无法删除）。

## 安全建议

旧内部文档处于公开状态的窗口期约数十分钟，期间任何人可通过 raw 链接读取，其中含 MySQL root 口令与服务器 IP。建议把该口令轮换一次，并复核部署密钥是否需要重新生成。

## 复现推送命令

```powershell
# GitHub（需覆盖本机 ghfast 改写规则）
git -c "url.https://ghfast.top/https://github.com/.insteadof=https://github.com__disabled" `
    push "https://x-access-token:<令牌>@github.com/lichong94/TermX.git" main

# Gitee（账号密码走 HTTPS，无需 SSH 公钥）
git push "https://lichong2:<密码>@gitee.com/lichong2/termx.git" main
```

Gitee 公开设置路径：仓库 → 管理 → 仓库设置 → 基本信息 → 是否开源选「开源（所有人可见）」→ 保存。实测提示「更新成功！」，接口复核 `public=true`、`language=Python`。
