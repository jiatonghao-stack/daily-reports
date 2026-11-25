#!/bin/zsh
# 一键安装 macOS 开机与早间自动同步服务（将云端生成的早报自动拉取到 文稿/每日报告）
set -euo pipefail

PLIST_PATH="$HOME/Library/LaunchAgents/com.ccjiatonghao.daily-ai-report-sync.plist"
SYNC_SCRIPT="/Users/ccjiatonghao/Documents/每日报告/.automation/sync_reports.sh"

chmod +x "$SYNC_SCRIPT"
chmod +x "/Users/ccjiatonghao/Documents/每日报告/.automation/generate_daily_report.py"
mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST_PATH" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.ccjiatonghao.daily-ai-report-sync</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string>
        <string>/Users/ccjiatonghao/Documents/每日报告/.automation/sync_reports.sh</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>StartInterval</key>
    <integer>600</integer>
    <key>StandardOutPath</key>
    <string>/Users/ccjiatonghao/Documents/每日报告/.automation/sync.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/ccjiatonghao/Documents/每日报告/.automation/sync.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"
echo "✅ 已启用 Mac 自动同步：电脑开机登录时立即拉取最新早报，运行期间每 10 分钟静默同步一次。"
