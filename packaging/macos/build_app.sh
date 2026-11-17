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
  <key>CFBundleName</key><string>AVD Flagship</string>
  <key>CFBundleDisplayName</key><string>AVD Flagship</string>
  <key>CFBundleIdentifier</key><string>com.hyht.avd-flagship</string>
  <key>CFBundleVersion</key><string>2.0.0</string>
  <key>CFBundleShortVersionString</key><string>2.0.0</string>
  <key>CFBundleExecutable</key><string>avd-flagship</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

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
  --include='examples/' --include='examples/**' \
  --include='docs/' --include='docs/**' \
  --include='LICENSES/' --include='LICENSES/**' \
  --include='cookies/' --exclude='cookies/*.txt' --exclude='cookies/*.rtf' \
  --exclude='cookies/*.har' --include='cookies/**' \
  --include='README.md' --include='README-FLAGSHIP.md' --include='CAPABILITIES.md' \
  --include='NOTICE.md' --include='LICENSE' --include='作者信息' \
  --include='PLATFORM_SUPPORT.md' --include='CHANGELOG.md' --include='VERSION' \
  --include='pyproject.toml' --include='requirements.txt' --include='requirements.lock' \
  --include='config.example.json' \
  --include='setup-asr.command' --include='setup-asr.bat' \
  --include='setup-windows-runtime.bat' \
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

# 出包自检（二）：随包第三方组件必须与 NOTICE/SCA 对得上。
# r5 抓到的真问题：runtime 装了 23 个包，NOTICE 只声明 8 个，15 个组件长期
# 未履行署名义务。源码仓 CI 守不住这件事（runtime/ 被 gitignore，只会 SKIP），
# 所以把门禁钉在打包产物上：产物里没有 runtime 也算失败。
if python3 "$ROOT/verify_supply_chain.py" --root "$CONTENTS/Resources/app" --strict \
    >/tmp/avd_supply.log 2>&1; then
  echo "supply_chain: OK"
else
  cat /tmp/avd_supply.log
  echo "FATAL: NOTICE/SCA 与随包组件不一致（或产物缺 runtime），已中止打包" >&2
  exit 1
fi

# 修正 launcher 指向内嵌 Resources/app
# D23：Finder 双击时 PATH 里只有系统 python3（macOS 自带 3.9，yt-dlp 已不支持）。
# 因此必须显式按 3.11+ 搜索：随包 runtime/python → 用户安装路径 → PATH；
# 全都找不到时弹原生对话框说明，而不是在终端里静默崩掉。
cat > "$CONTENTS/MacOS/avd-flagship" <<'LAUNCH'
#!/usr/bin/env bash
ROOT="$(cd "$(dirname "$0")/../Resources/app" && pwd)"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT"

min_ok() { "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; }

PY=""
# 1) 随包便携运行时（materialize_runtime.py 装配后位于 runtime/python）
for cand in "$ROOT/runtime/python/bin/python3" "$ROOT/runtime/python/bin/python" \
            "$ROOT/runtime/python/python.exe"; do
  if [ -x "$cand" ] && min_ok "$cand"; then PY="$cand"; break; fi
done
# 2) 用户常见安装位置（双击启动时通常不在 PATH）
if [ -z "$PY" ]; then
  for cand in "$HOME/.local/bin/python3.13" "$HOME/.local/bin/python3.12" \
              "$HOME/.local/bin/python3.11" /opt/homebrew/bin/python3.13 \
              /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
              /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
              /usr/local/bin/python3.11; do
    if [ -x "$cand" ] && min_ok "$cand"; then PY="$cand"; break; fi
  done
fi
# 3) PATH 中的具名二进制（不信任裸 python3，它可能是 3.9）
if [ -z "$PY" ]; then
  for cand in python3.13 python3.12 python3.11; do
    found="$(command -v "$cand" 2>/dev/null || true)"
    if [ -n "$found" ] && min_ok "$found"; then PY="$found"; break; fi
  done
fi

if [ -z "$PY" ]; then
  echo "AVD Flagship 需要 Python 3.11+（系统自带的 3.9 无法运行内嵌 yt-dlp 2026.10.26）。" >&2
  echo "请安装 Python 3.11 或 3.12（python.org 或 Homebrew），然后重新打开本应用。" >&2
  echo "本地语音识别另需运行 setup-asr.command 准备 .venv-asr 运行时。" >&2
  osascript -e 'display dialog "AVD Flagship 需要 Python 3.11+。请安装 Python 3.11 或 3.12 后重新打开本应用。语音识别另需运行 setup-asr.command。" buttons {"好"} default button 1 with icon caution' >/dev/null 2>&1 || true
  exit 1
fi

exec "$PY" -c "from boot import main; raise SystemExit(main())" "$@"
LAUNCH
chmod +x "$CONTENTS/MacOS/avd-flagship"

# D23：模型随包、ASR 运行时不随包 —— 这个组合必须写明，不能让用户以为开箱即转写。
if [[ ! -d "$CONTENTS/Resources/app/.venv-asr" ]]; then
  echo "NOTE: 未内嵌 ASR 运行时（.venv-asr）。Apple Silicon 可运行 setup-asr.command 生成；" \
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
