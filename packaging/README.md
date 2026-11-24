# 片语 安装包脚手架

本目录提供**可签名安装包的骨架**；真正的 Apple 公证 / Windows 代码签名需在持证机器上完成。

## 形态

| 平台 | 脚手架 | 产出 |
|------|--------|------|
| macOS | `macos/build_app.sh` | `.app` 目录 + 可选 `.dmg` |
| Windows | `windows/build_msi.ps1` | 便携目录；WiX/MSI 步骤见注释 |

## 本机快速包（无需证书）

```bash
# macOS
bash packaging/macos/build_app.sh

# Windows（PowerShell）
pwsh packaging/windows/build_msi.ps1
```

## 签名 / 公证（持证后）

1. 用 Developer ID Application 对 `.app` 执行 `codesign --deep --force --options runtime`
2. `notarytool submit` + `stapler staple`
3. Windows：用 `signtool` 对 exe/msi 签名

环境变量：

- `AVD_CODESIGN_ID` — macOS 证书 Common Name
- `AVD_NOTARY_PROFILE` — notarytool 钥匙串配置名
- `AVD_WIN_CERT` — Windows PFX 路径

未设置时脚本只打**未签名**包，并在产物旁写 `UNSIGNED.txt`。
