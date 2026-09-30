# Plasmora 分支与发布流程

Plasmora 由单人维护，使用三个层级的分支：

| 分支 | 用途 |
| --- | --- |
| `main` | 已发布、可安装的稳定版本；版本标签 `vX.Y.Z` 从这里创建。 |
| `develop` | 日常开发的集成分支。 |
| `codex/<主题>`、`feature/<主题>`、`fix/<主题>` | 一项功能或修复对应一个短期分支，从 `develop` 创建。 |

## 开发变更

1. 从最新 `develop` 创建短期分支。不要直接向 `main` 或 `develop` 推送代码。
2. 本机运行 `python -B -m unittest discover -p test_*.py` 和 `node test_frontend_logic.js`。
3. 创建目标为 `develop` 的 Pull Request，等待 CI 的 `tests` 通过后用 **Squash and merge** 合并。合并后删除短期分支。
4. 紧急发布修复可从 `main` 创建 `fix/<主题>`；修复发布后再将改动通过 PR 同步到 `develop`。

## 发布版本

1. 在 `develop` 更新版本号、README 和安装脚本，确认测试与安装包可用。
2. 创建从 `develop` 到 `main` 的 Pull Request；CI 通过后合并。单人维护不要求额外审批。
3. 从 `main` 的发布提交创建 `vX.Y.Z` 标签，并把对应安装包上传到 GitHub Releases。
4. 将发布后的 `main` 通过 PR 同步回 `develop`（如两者已有相同改动，可跳过）。

`main` 和 `develop` 禁止强制推送及删除。PR 和 CI 是合并门槛；分支保护不要求第二个人批准。
