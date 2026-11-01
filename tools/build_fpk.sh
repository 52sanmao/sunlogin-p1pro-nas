#!/usr/bin/env bash
# 一键把源码打成飞牛应用包 p1pro.fpk
#
#   bash tools/build_fpk.sh
#
# 需要网络（第一次会下载 fnpack）。之后的构建全部离线。
# 产物：项目根目录下的 p1pro.fpk，可直接在飞牛应用中心"手动安装"。

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="1.2.3"
CACHE="${ROOT}/.cache"
BIN="${CACHE}/fnpack"

cd "$ROOT"

# ---- 1. 准备 fnpack ----
if [ ! -x "$BIN" ]; then
    mkdir -p "$CACHE"
    case "$(uname -s)" in
        Linux)  PLAT="linux-amd64" ;;
        Darwin) PLAT="darwin-amd64"
                [ "$(uname -m)" = "arm64" ] && PLAT="darwin-arm64" ;;
        MINGW* | MSYS* | CYGWIN*) PLAT="windows-amd64" ;;
        *) echo "不认识的系统：$(uname -s)" >&2; exit 1 ;;
    esac
    if [ "$PLAT" = "linux-amd64" ] && [ "$(uname -m)" = "aarch64" ]; then
        PLAT="linux-arm64"
    fi
    URL="https://static2.fnnas.com/fnpack/fnpack-${VERSION}-${PLAT}"
    echo "==> 下载 fnpack：$URL"
    curl -fsSL -o "$BIN" "$URL"
    chmod +x "$BIN"
fi

# ---- 2. 规整脚本：必须是 LF、必须有可执行位 ----
# Windows 上编辑过的脚本常常是 CRLF，装到飞牛上会报
# "bad interpreter: /bin/bash^M"，打包前统一一次。
echo "==> 规整 cmd/ 脚本换行与权限"
for f in cmd/*; do
    [ -f "$f" ] || continue
    if grep -q $'\r' "$f" 2>/dev/null; then
        tr -d '\r' <"$f" >"$f.tmp" && mv "$f.tmp" "$f"
    fi
    chmod +x "$f"
done

# ---- 3. 语法自检：脚本写错了别等装到 NAS 上才发现 ----
echo "==> 检查 shell 脚本语法"
for f in cmd/*; do
    [ -f "$f" ] || continue
    bash -n "$f" || { echo "语法错误：$f" >&2; exit 1; }
done

echo "==> 检查 Python 语法"
PYBIN="$(command -v python3 || command -v python || echo python)"
"$PYBIN" -m compileall -q app/server >/dev/null || {
    echo "app/server 下有 Python 语法错误" >&2
    exit 1
}
find app/server -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# ---- 4. 打包 ----
echo "==> fnpack build"
"$BIN" build

echo ""
echo "完成：${ROOT}/p1pro.fpk"
ls -lh p1pro.fpk 2>/dev/null || true
