# TermX

轻量级 Windows 终端工具，SecureCRT 的替代方案：SSH2 / 串口 / 本地 Shell 多协议，会话管理、脚本引擎、SFTP 文件传输、端口转发、X11 转发，绿色免安装、零外部依赖，支持 IPv4 / IPv6 双栈。

- 官网与下载：https://tx.ddisk.cn
- 运行平台：Windows 10 / 11 · x64
- 当前版本：**v1.4.20（缺陷修复版）**
- 许可形态：绿色免安装，不写注册表，无需管理员权限

## 功能特性

- **全自包含 · 零外部依赖**：内置 OpenSSH、plink、VcXsrv 与 VC 运行库，解压即用
- **多协议会话**：SSH2 / Telnet / 串口 Serial / 本地 Shell 全覆盖，兼容 SSH1
- **6 套界面主题**：SecureCRT 经典、浅色、深色、Windows Terminal·午夜、MobaXterm·晴空、iTerm2·Solarized
- **会话管理器**：树形分组与收藏，支持导入 SecureCRT `.ini` 与 JSON，导出 CSV
- **多标签与克隆**：标签多开、拖拽排序，一键克隆会话，广播输入同时下发到所有会话
- **脚本引擎与批量下发**：内置脚本编辑器，录制回放、批量命令下发、变量替换（支持中文变量名）
- **批量运维**：多设备批量执行、登录后自动关闭分页（华为 `screen-length 0 temporary` / 华三 `screen-length disable`）
- **SFTP / 文件传输**：内置 SFTP 标签，支持 Zmodem / lrzsz、端口转发、X11 转发
- **IPv6 与安全**：原生 IPv4 / IPv6 双栈连接，SSH 密钥生成，会话锁定，密码加密存储

## 下载

| 文件 | 体积 | 说明 |
| --- | --- | --- |
| [`TermX_单文件版.exe`](工程/dist/TermX_单文件版.exe) | 69.1 MB | 单个 exe，双击即用，适合分发或随身携带 |
| [`TermX_绿色免安装包.zip`](工程/dist/TermX_绿色免安装包.zip) | 61.0 MB | 解压即用，启动更快、占用更省，推荐日常使用 |

SHA256 校验值：

```
71997e4f89069159246443993f02cfd389eaf7ff233aa3a6df1374115d0975dd  TermX_单文件版.exe
ff0bd8910ed51f6ee165beb051f0c5280a73eaf33bb95ba7fc6e6d894ec4ba48  TermX_绿色免安装包.zip
```

> 大文件托管在 GitHub 仓库内；国内下载建议走官网 https://tx.ddisk.cn/downloads ，带宽更稳定。

## 快速上手

1. 绿色包解压到任意目录，或直接运行单文件版 exe
2. 新建会话：点击「快速连接」，填写主机、端口与协议（SSH2 / 串口 / 本地 Shell），或导入既有配置
3. 连接并管理：多标签并行操作，`F9` 切换主题，通过会话管理器保存常用主机
4. 首次运行会要求选择数据目录（默认建议 `D:\TermX`），除该目录外不创建任何文件夹；换机时整目录拷走即可沿用会话与配置

命令行：

```
TermX.exe --selftest --out 自检结果.txt     # 离线自检（44 项）
TermX.exe --smoke                          # 启动冒烟测试
TermX.exe --data-dir                       # 查看当前运行目录
TermX.exe --data-dir-set 路径               # 指定运行目录
```

## 源码结构

```
工程/src/termx/
  app.py           入口、命令行参数、自检用例（44 项）
  main_window.py   主窗口、标签管理、菜单与快捷键
  sessions.py      会话库 / 账号库、密码加密（DPAPI + OBF2 兜底）、变量替换
  connections.py   SSH / Telnet / 串口 / 本地 Shell / 代理隧道
  terminal.py      终端渲染、主题、滚动缓冲、弱化色渲染
  ansi.py          ANSI 转义序列解析
  sftp.py          SFTP 与文件传输
  dialogs.py       各类对话框（会话属性、账号管理、批量下发等）
  config.py        配置读写、256 色映射、版本号
  conpty.py        ConPTY 本地终端后端
工程/docs/使用说明.txt       随包使用说明
工程/dist/                   安装包与产物
文档/v1.4.20-缺陷修复记录.md  本轮 14 项缺陷的根因、改法与验证证据
```

开发环境：Python 3.13 + tkinter + ConPTY + PyInstaller。Windows 下可直接以源码方式运行：

```
python -m termx.app
```

## v1.4.20 修复内容

按第三方测试报告的 14 条缺陷逐项修复，自检由 30 项扩展到 44 项，源码与两个打包产物均 44/44 通过。要点：

- SecureCRT `.ini` 导入端口解析（8 位十六进制 DWORD / `0x` 前缀 / 十进制）
- 本地 Shell 路径含空格时的 argv 与引号处理
- 删除分组按完整路径段匹配，避免误删同前缀分组
- 会话库 / 账号库保存失败可见化（`os.replace` 原子写 + 界面提示）
- SGR `2`（弱化）渲染、`21` / `22` 语义修正
- 256 色索引越界与非数字输入兜底
- 串口流控写入 DCB（none / xonxoff / hardware）
- 代理隧道连接失败错误上报（避免界面无限重连）
- ConPTY 启动失败分支回收管道句柄
- SSH-1 连接界面展示的命令行密码遮罩
- DPAPI 失败时以机器绑定混淆（`OBF2:`）兜底，不再落空串
- 防掉线字符串按会话编码写入
- SOCKS5 握手读满 N 字节
- 变量名支持中文、空格、短横线，修正 `${}` 分支

逐项根因、验证命令与能力边界见 [文档/v1.4.20-缺陷修复记录.md](文档/v1.4.20-缺陷修复记录.md)。

## 已知限制

- 随包 plink 为 Release 0.67：不支持 `-pwfile`，SSH-1 连接密码只能做界面遮罩，无法彻底移出命令行；新版 PuTTY 已移除 SSH-1 协议
- 串口流控、SOCKS5 代理为代码级与单元测试级验证，未接入真实硬件与代理服务做长时间压测

## 许可与声明

源码公开供学习与二次开发参考。随包携带的第三方组件（OpenSSH、PuTTY/plink、VcXsrv、字体、VC 运行库）版权归各自作者所有，遵循其原始许可。

使用本软件连接设备、执行批量命令产生的一切后果由使用者自行承担。
