#!/usr/bin/env bash
# AVD Flagship — macOS .app 脚手架（未签名可跑；签名需 AVD_CODESIGN_ID）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DIST="$ROOT/dist/macos"
APP="$DIST/AVD Flagship.app"
CONTENTS="$APP/Contents"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"

cat > "$CONTENTS/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>片语</string>
  <key>CFBundleDisplayName</key><string>片语</string>
  <key>CFBundleIdentifier</key><string>com.hyht.avd-flagship</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundleIconName</key><string>AppIcon</string>
  <key>CFBundleVersion</key><string>2.0.0</string>
  <key>CFBundleShortVersionString</key><string>2.0.0</string>
  <key>CFBundleExecutable</key><string>avd-flagship</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <!-- 壳脚本常驻服务：不进 Dock，避免图标一直弹跳且点了没反应 -->
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

# 应用图标：由 static/brand/icon.svg 渲染的 AppIcon.icns（1024 基底 + 全尺寸 iconset）
cp "$ROOT/packaging/macos/AppIcon.icns" "$CONTENTS/Resources/AppIcon.icns"

# 把源码树复制进 Resources
# SEC-01：发布包绝不含本机登录态。cookies/ 只保留说明与占位模板，
# .state/（Cookie 可写副本）、outputs/（任务产物与任务库）、evidence/、
# 任何 *.sqlite3 / cookies.txt 一律排除。
# D32：由黑名单改为白名单 —— 只放运行所需的目录与文件，报告 HTML、
# 验证脚本、junk 文件、测试与缓存不会因为「忘了 exclude」而混进发布包。
echo "$ROOT" > "$CONTENTS/Resources/SOURCE_ROOT.txt"
rsync -a --delete \
  --include='app.py' --include='boot.py' --include='server.py' --include='doctor.py' \
  --include='fusion/' --include='fusion/**' \
  --include='downloader/' --include='downloader/**' \
  --include='static/' --include='static/**' \
  --include='vendor/' --include='vendor/**' \
  --include='yt_dlp_plugins/' --include='yt_dlp_plugins/**' \
  --include='tools/' --include='tools/**' \
  --include='models/' --include='models/**' \
  --include='runtime/' --include='runtime/**' \
  --include='docs/' --include='docs/**' \
  --include='cookies/' --exclude='cookies/*.txt' --exclude='cookies/*.rtf' \
  --exclude='cookies/*.har' --include='cookies/**' \
  --include='README.md' --include='README-FLAGSHIP.md' --include='CAPABILITIES.md' \
  --include='NOTICE.md' --include='LICENSE' \
  --include='PLATFORM_SUPPORT.md' --include='CHANGELOG.md' --include='VERSION' \
  --include='pyproject.toml' --include='requirements.txt' --include='requirements.lock' \
  --include='config.example.json' \
  --exclude='*' \
  "$ROOT/" "$CONTENTS/Resources/app/" 2>/dev/null || {
  mkdir -p "$CONTENTS/Resources/app"
  cp -R "$ROOT/fusion" "$ROOT/downloader" "$ROOT/server.py" "$ROOT/static" \
    "$ROOT/boot.py" "$ROOT/pyproject.toml" "$CONTENTS/Resources/app/" 2>/dev/null || true
  rm -f "$CONTENTS/Resources/app/cookies/"*.txt 2>/dev/null || true
}

# D15：不得把本机 .venv-asr 绝对路径软链打进包；产物内断链/绝对软链直接失败
if [[ -e "$CONTENTS/Resources/app/.venv-asr" ]]; then
  echo "FATAL: 产物含 .venv-asr（不可重定位），已中止打包" >&2
  exit 1
fi
bad_links=0
while IFS= read -r link; do
  [[ -z "$link" ]] && continue
  target="$(readlink "$link" || true)"
  case "$target" in
    /*)
      echo "FATAL: 绝对路径软链 $link -> $target" >&2
      bad_links=1
      ;;
  esac
  if [[ ! -e "$link" ]]; then
    echo "FATAL: 断链 $link -> $target" >&2
    bad_links=1
  fi
done < <(find "$CONTENTS/Resources/app" -type l 2>/dev/null || true)
if [[ "$bad_links" -ne 0 ]]; then
  exit 1
fi

# 出包自检：任何真实凭据残留都直接让构建失败
if python3 "$ROOT/tools/secrets_scan.py" --root "$CONTENTS/Resources/app" >/tmp/avd_secrets.log 2>&1; then
  echo "secrets_scan: OK"
else
  cat /tmp/avd_secrets.log
  echo "FATAL: 发布包含真实 Cookie/凭据，已中止打包" >&2
  exit 1
fi

# 修正 launcher 指向内嵌 Resources/app
# D23：Finder 双击时 PATH 里只有系统 python3（macOS 自带 3.9，yt-dlp 已不支持）。
# 因此必须显式按 3.11+ 搜索：随包 runtime/python → 用户安装路径 → PATH；
# 全都找不到时弹原生对话框说明，而不是在终端里静默崩掉。
cat > "$CONTENTS/MacOS/avd-flagship" <<'LAUNCH'
#!/usr/bin/env bash
# Finder 双击入口：单实例 + 立即开浏览器；日志写入 ~/Library/Logs/AVD-Flagship/
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../Resources/app" && pwd)"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1
cd "$ROOT"

LOG_DIR="${HOME}/Library/Logs/AVD-Flagship"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/launch.log"
exec > >(tee -a "$LOG") 2>&1
echo "---- $(date '+%Y-%m-%d %H:%M:%S') launch ----"
echo "ROOT=$ROOT"

open_ui() {
  local url="${1:-http://127.0.0.1:8760/}"
  echo "open $url"
  open "$url" >/dev/null 2>&1 || true
}

# 已在跑：直接打开页面，避免 Dock 二次弹跳 / 端口争抢
if curl -fsS --max-time 1 "http://127.0.0.1:8760/" >/dev/null 2>&1; then
  open_ui "http://127.0.0.1:8760/"
  osascript -e 'display notification "服务已在运行，已重新打开工作台" with title "片语"' >/dev/null 2>&1 || true
  exit 0
fi

min_ok() { "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; }

PY=""
for cand in "$ROOT/runtime/python/bin/python3" "$ROOT/runtime/python/bin/python" \
            "$HOME/.local/bin/python3.13" "$HOME/.local/bin/python3.12" \
            "$HOME/.local/bin/python3.11" /opt/homebrew/bin/python3.13 \
            /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
            /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
            /usr/local/bin/python3.11; do
  if [ -x "$cand" ] && min_ok "$cand"; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  for cand in python3.13 python3.12 python3.11; do
    found="$(command -v "$cand" 2>/dev/null || true)"
    if [ -n "$found" ] && min_ok "$found"; then PY="$found"; break; fi
  done
fi

if [ -z "$PY" ]; then
  osascript -e 'display dialog "片语 需要 Python 3.11+。\n请安装 Python 3.11/3.12 后重试。\n日志：~/Library/Logs/AVD-Flagship/launch.log" buttons {"好"} default button 1 with icon caution' >/dev/null 2>&1 || true
  exit 1
fi
echo "PY=$PY"

# 后台启动服务；启动器自己轮询就绪后开浏览器（Finder 双击无终端）
# AVD_NO_BROWSER=1：避免 boot.py 再开一次浏览器造成双窗口
export AVD_NO_BROWSER=1
"$PY" -u -c "from boot import main; raise SystemExit(main())" "$@" &
SPID=$!
echo "server_pid=$SPID"

READY_URL=""
for _ in $(seq 1 40); do
  if ! kill -0 "$SPID" 2>/dev/null; then
    osascript -e "display dialog \"片语 启动失败。\n请查看日志：\n$LOG\" buttons {\"好\"} default button 1 with icon stop" >/dev/null 2>&1 || true
    exit 1
  fi
  for port in $(seq 8760 8799); do
    if curl -fsS --max-time 0.4 "http://127.0.0.1:${port}/" >/dev/null 2>&1; then
      READY_URL="http://127.0.0.1:${port}/"
      break 2
    fi
  done
  sleep 0.25
done

if [ -z "$READY_URL" ]; then
  osascript -e "display dialog \"片语 启动超时（未监听端口）。\n日志：\n$LOG\" buttons {\"好\"} default button 1 with icon stop" >/dev/null 2>&1 || true
  exit 1
fi

open_ui "$READY_URL"
osascript -e 'display notification "工作台已打开" with title "片语"' >/dev/null 2>&1 || true

# 等服务进程结束（LSUIElement=true，不占 Dock）
wait "$SPID"
LAUNCH
chmod +x "$CONTENTS/MacOS/avd-flagship"

# D23：模型随包、ASR 运行时不随包 —— 这个组合必须写明，不能让用户以为开箱即转写。
if [[ ! -d "$CONTENTS/Resources/app/.venv-asr" ]]; then
  echo "NOTE: 未内嵌 ASR 运行时（.venv-asr）。Apple Silicon 可自行装配 .venv-asr；" \
       "模型权重已随包，无需重新下载。" >&2
fi

if [[ -n "${AVD_CODESIGN_ID:-}" ]]; then
  codesign --deep --force --options runtime --sign "$AVD_CODESIGN_ID" "$APP"
  echo "signed with $AVD_CODESIGN_ID"
  if [[ -n "${AVD_NOTARY_PROFILE:-}" ]]; then
    ditto -c -k --keepParent "$APP" "$DIST/AVD-Flagship.zip"
    xcrun notarytool submit "$DIST/AVD-Flagship.zip" --keychain-profile "$AVD_NOTARY_PROFILE" --wait
    xcrun stapler staple "$APP" || true
  fi
else
  echo "UNSIGNED — set AVD_CODESIGN_ID to codesign" > "$DIST/UNSIGNED.txt"
fi

echo "Built: $APP"
