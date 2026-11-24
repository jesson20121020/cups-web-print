#!/bin/bash
# print-file.sh —— 把本地文件交给 NAS 上的「Web 打印服务」(cups-web-print) 打印
#
# 走服务自己的 HTTP API，和网页点“打印”完全等价：
#   POST /api/upload  → 上传 + 服务端生成预览 PDF（图片/Office 会自动转换）
#   POST /api/print   → 提交 CUPS 任务
#   GET  /api/jobs    → 轮询任务状态
#
# 用法:
#   print-file.sh <文件路径> [选项]
#   print-file.sh --status                      # 只看服务/打印机/队列状态
#
# 选项:
#   --printer NAME      队列名（默认自动挑第一台 IPP 队列，通常是 TS3380-IPPS）
#   --copies N          份数（1-99，默认 1）
#   --mono              黑白（默认彩色）
#   --no-composite-black  彩色时不让黑色由彩墨合成（默认彩色下强制 ColorModel=RGB 彩墨黑）
#   --simplex           单面（默认双面长边，与网页默认一致）
#   --duplex-short      双面短边
#   --paper SIZE        A4(默认)/A3/A5/A6/B4/B5/4x6/5x7/3.5x5/8x10
#   --paper-type TYPE   plain(默认)/photo/glossy/matte/cardstock/labels/auto
#   --pages "1-5 8"     页面范围（PDF 或转换后的文档；空格分隔，不支持逗号）
#   --scaling MODE      fit(默认)/none/fill/auto/auto-fit
#   --orientation O     portrait(默认)/landscape
#   --no-print          只上传不打印（返回服务端文件名，便于预览或后续再打）
#   --dry-run           只校验参数并打印将要提交的 JSON/命令，不联服务
#   --wait SECONDS      提交后等待打印完成的秒数（默认 180，0=只提交不等待）
#   --json              末尾额外输出一行 JSON（给 agent 解析）
#   --service URL       覆盖服务地址（默认 http://127.0.0.1:5000）
#   --force             打印机离线时仍然提交（默认拒绝，避免任务卡在队列里）
#
# 退出码: 0 成功提交且（等待时）打印完成；1 参数/文件问题；2 服务不可用；
#         3 打印机离线；4 上传失败；5 提交失败；6 打印失败/超时

set -uo pipefail

SERVICE="http://127.0.0.1:5000"
PRINTER=""
COPIES=1
COLOR="color"
COMPOSITE_BLACK=1   # 彩色默认：显式 ColorModel=RGB，黑色由 CMY 彩墨合成
DUPLEX="two-sided-long-edge"
PAPER="A4"
PAGES=""
SCALING="fit"
ORIENTATION="portrait"
DO_PRINT=1
DRY_RUN=0
WAIT=180
AS_JSON=0
FORCE=0
FILE=""
PAPER_TYPE="plain"

ALLOWED="pdf txt doc docx ppt pptx xls xlsx rtf jpg jpeg png gif bmp svg"

die() { echo "❌ $*" >&2; exit "${2:-1}"; }

usage() { sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
    case "$1" in
        --status) SERVICE="http://127.0.0.1:5000"; PRINTER="__STATUS__"; shift ;;
        --printer) PRINTER="${2:-}"; shift 2 ;;
        --copies) COPIES="${2:-1}"; shift 2 ;;
        --mono) COLOR="mono"; shift ;;
        --no-composite-black) COMPOSITE_BLACK=0; shift ;;
        --composite-black) COMPOSITE_BLACK=1; shift ;;
        --simplex) DUPLEX="one-sided"; shift ;;
        --duplex-short) DUPLEX="two-sided-short-edge"; shift ;;
        --paper) PAPER="${2:-A4}"; shift 2 ;;
        --paper-type) PAPER_TYPE="${2:-plain}"; shift 2 ;;
        --pages) PAGES="${2:-}"; shift 2 ;;
        --scaling) SCALING="${2:-fit}"; shift 2 ;;
        --orientation) ORIENTATION="${2:-portrait}"; shift 2 ;;
        --no-print) DO_PRINT=0; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --wait) WAIT="${2:-180}"; shift 2 ;;
        --json) AS_JSON=1; shift ;;
        --service) SERVICE="${2:-}"; shift 2 ;;
        --force) FORCE=1; shift ;;
        -h|--help) usage ;;
        -*) die "未知参数：$1" ;;
        *) FILE="$1"; shift ;;
    esac
done

# ---------- 小工具：从 JSON 取字段 / 格式化输出 ----------
json_get() {
    python3 -c '
import json,sys
raw=sys.stdin.read()
try:
    d=json.loads(raw)
except Exception:
    sys.exit(2)
for p in sys.argv[1].split("."):
    if isinstance(d,list):
        try: d=d[int(p)]
        except Exception: sys.exit(3)
    elif isinstance(d,dict) and p in d:
        d=d[p]
    else:
        sys.exit(3)
print("" if d is None else d)
' "$1" 2>/dev/null
}

api_get() { curl -sS -m 10 "$SERVICE$1"; }

# ---------- --status：只看状态 ----------
if [ "$PRINTER" = "__STATUS__" ]; then
    printers_json="$(api_get /api/printers)" || die "打印服务不可达：$SERVICE" 2
    [ -n "$printers_json" ] || die "打印服务无响应：$SERVICE" 2
    echo "服务：$SERVICE"
    echo "队列："
    python3 - "$printers_json" <<'PY'
import json,sys
data=json.loads(sys.argv[1])
ps=data.get("printers") or []
if not ps: print("  （服务没有返回任何 CUPS 队列）")
for p in ps:
    print(f"  - {p.get('name')}  status={p.get('status')}  uri={p.get('uri')}")
PY
    first="$(printf '%s' "$printers_json" | python3 -c '
import json,sys
ps=json.load(sys.stdin).get("printers") or []
ipp=[p for p in ps if str(p.get("uri","")).startswith(("ipp://","ipps://"))]
print((ipp or ps)[0]["name"]) if ps else print("")')"
    if [ -n "$first" ]; then
        st="$(api_get "/api/printer/$first/status")"
        echo "打印机 $first：$(printf '%s' "$st" | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("online_status=%s status=%s" % (d.get("online_status"), d.get("status")))
if d.get("offline_reason"):
    print("  原因：" + str(d.get("offline_reason")))')"
    fi
    jobs="$(api_get /api/jobs)"
    echo "任务：$(printf '%s' "$jobs" | python3 -c '
import json,sys
js=json.load(sys.stdin).get("jobs") or []
if not js:
    print("队列为空")
for j in js:
    print("  - %s %d%%  %s  %s" % (j.get("status"), int(j.get("progress") or 0),
                                   j.get("filename"), j.get("message") or ""))')"
    exit 0
fi

# ---------- 参数校验 ----------
[ -n "$FILE" ] || die "缺少文件路径（或 --status）" 1
[ -f "$FILE" ] || die "文件不存在：$FILE" 1
case "$COPIES" in ''|*[!0-9]*) die "--copies 必须是数字" 1 ;; esac
[ "$COPIES" -ge 1 ] && [ "$COPIES" -le 99 ] || die "--copies 必须在 1-99 之间" 1

ext="$(printf '%s' "${FILE##*.}" | tr '[:upper:]' '[:lower:]')"
case " $ALLOWED " in
    *" $ext "*) ;;
    *) die "不支持的文件类型 .$ext（支持：$ALLOWED）。iPhone 的 HEIC 请先转成 JPG（分享时选“JPEG”或 convert 一下）" 1 ;;
esac

size=$(stat -c %s "$FILE" 2>/dev/null || echo 0)
if [ "$size" -gt $((100*1024*1024)) ]; then
    die "文件超过 100MB，服务端会拒收：$FILE" 1
fi

if [ -n "$PAGES" ] && printf '%s' "$PAGES" | grep -q ','; then
    die "页面范围用空格分隔，不支持逗号（例：--pages \"1-5 8\"）" 1
fi

# ---------- dry-run：只打印将要提交的内容 ----------
if [ "$DRY_RUN" = 1 ]; then
    printf '文件：%s（%s）\n' "$FILE" "$(python3 -c "s=$size; print(('%.1f MB' % (s/1048576)) if s>=1048576 else ('%.0f KB' % (s/1024)))")"
    printf '上传：POST %s/api/upload  (multipart file=@%s)\n' "$SERVICE" "$FILE"
    printf '提交：POST %s/api/print  body=\n' "$SERVICE"
    python3 - "$FILE" "$COPIES" "$COLOR" "$DUPLEX" "$PAPER" "$PAPER_TYPE" "$PAGES" "$ORIENTATION" "$SCALING" "$PRINTER" "$COMPOSITE_BLACK" <<'PY'
import json,sys,os
f,copies,color,duplex,paper,ptype,pages,orient,scaling,printer,cblack=sys.argv[1:12]
body={
 "filename":"<服务端返回的文件名>",
 "printer":printer or "<自动选择>",
 "color_mode":color,"duplex":duplex,"orientation":orient,
 "paper_size":paper,"paper_type":ptype,"copies":int(copies),
 "page_range":pages or None,"mirror":False,"print_scaling":scaling,
 "composite_black":(color=="color" and cblack=="1"),
}
print(json.dumps(body,ensure_ascii=False,indent=2))
print(f"# 原始文件：{os.path.basename(f)}")
PY
    exit 0
fi

# ---------- 1. 服务是否活着 ----------
printers_json="$(api_get /api/printers)" || die "打印服务不可达：$SERVICE（检查 systemctl is-active cups-web-print / 端口 5000）" 2
[ -n "$printers_json" ] || die "打印服务无响应：$SERVICE" 2

# ---------- 2. 选打印机 ----------
if [ -z "$PRINTER" ]; then
    PRINTER="$(printf '%s' "$printers_json" | python3 -c '
import json,sys
ps=json.load(sys.stdin).get("printers") or []
ipp=[p for p in ps if str(p.get("uri","")).startswith(("ipp://","ipps://"))]
print(((ipp or ps)+[{}])[0].get("name","") if ps else "")')"
fi
[ -n "$PRINTER" ] || die "服务没有可用的 CUPS 队列" 2

# ---------- 3. 打印机必须在线（离线就别提交，免得任务卡死在队列里） ----------
status_json="$(api_get "/api/printer/$PRINTER/status")"
online="$(printf '%s' "$status_json" | json_get online_status)"
if [ "$online" != "online" ] && [ "$FORCE" != 1 ]; then
    reason="$(printf '%s' "$status_json" | json_get offline_reason)"
    [ -n "$reason" ] || reason="打印机 $PRINTER 当前状态：${online:-unknown}"
    echo "❌ 打印机不在线，已取消提交：$reason" >&2
    echo "   （确要提交请加 --force；恢复后卡住的任务会自动打印）" >&2
    exit 3
fi
echo "🖨  打印机：$PRINTER（$online）"

# ---------- 4. 上传 ----------
upload_json="$(curl -sS -m 120 -X POST -F "file=@$FILE" "$SERVICE/api/upload")" || die "上传失败：无法连接 $SERVICE" 4
ok="$(printf '%s' "$upload_json" | json_get success)"
remote_name="$(printf '%s' "$upload_json" | json_get filename)"
if [ "$ok" != "True" ] && [ "$ok" != "true" ]; then
    err="$(printf '%s' "$upload_json" | json_get error)"
    die "上传被拒：${err:-$upload_json}" 4
fi
[ -n "$remote_name" ] || die "上传响应异常：$upload_json" 4
warning="$(printf '%s' "$upload_json" | json_get warning)"
if [ -n "$warning" ]; then
    echo "⚠️  服务端转换告警：$warning" >&2
    [ "$DO_PRINT" = 1 ] && die "服务端没能把文件转成可打印的 PDF，已停止打印（$warning）" 4
fi
echo "📤 已上传：$remote_name"

if [ "$DO_PRINT" = 0 ]; then
    echo "（--no-print：未提交打印）"
    [ "$AS_JSON" = 1 ] && printf '{"uploaded": true, "filename": "%s", "printer": "%s"}\n' "$remote_name" "$PRINTER"
    exit 0
fi

# ---------- 5. 提交打印 ----------
body="$(python3 - "$remote_name" "$PRINTER" "$COPIES" "$COLOR" "$DUPLEX" "$PAPER" "$PAPER_TYPE" "$PAGES" "$ORIENTATION" "$SCALING" "$COMPOSITE_BLACK" <<'PY'
import json,sys
name,printer,copies,color,duplex,paper,ptype,pages,orient,scaling,cblack=sys.argv[1:12]
print(json.dumps({
 "filename":name,"printer":printer,"color_mode":color,"duplex":duplex,
 "orientation":orient,"paper_size":paper,"paper_type":ptype,"copies":int(copies),
 "page_range":(pages or None),"mirror":False,"print_scaling":scaling,
 "composite_black":(color=="color" and cblack=="1"),
},ensure_ascii=False))
PY
)"
print_json="$(curl -sS -m 60 -X POST -H 'Content-Type: application/json' -d "$body" "$SERVICE/api/print")" || die "提交失败：无法连接 $SERVICE" 5
p_ok="$(printf '%s' "$print_json" | json_get success)"
job_id="$(printf '%s' "$print_json" | json_get job_id)"
if [ "$p_ok" != "True" ] && [ "$p_ok" != "true" ]; then
    die "提交被拒：$(printf '%s' "$print_json" | json_get error)" 5
fi
cups_id="$(printf '%s' "$print_json" | json_get job.cups_job_id)"
echo "🧾 已提交：job=$job_id cups=$cups_id  $COLOR/$DUPLEX/$PAPER×$COPIES"

# ---------- 6. 等结果 ----------
final_status="submitted"
final_msg=""
if [ "$WAIT" -gt 0 ]; then
    deadline=$(( $(date +%s) + WAIT ))
    last_key=""
    while [ "$(date +%s)" -lt "$deadline" ]; do
        jobs_json="$(api_get /api/jobs)"
        line="$(printf '%s' "$jobs_json" | python3 -c '
import json,sys
jid=sys.argv[1]
for j in (json.load(sys.stdin).get("jobs") or []):
    if j.get("id")==jid:
        print("%s|%s|%s" % (j.get("status"), j.get("progress"), j.get("message") or ""))
        break
' "$job_id")"
        if [ -n "$line" ]; then
            st="${line%%|*}"; rest="${line#*|}"; prog="${rest%%|*}"; msg="${rest#*|}"
            # 进度按 25% 一档输出，避免刷屏（状态一变立刻输出）
            skin="${prog%%.*}"; [ -n "$skin" ] || skin=0
            key="$st:$((skin / 25))"
            if [ "$key" != "$last_key" ]; then
                echo "   … $st ${prog}% ${msg}"
                last_key="$key"
            fi
            case "$st" in
                completed|cancelled|failed|timeout|error)
                    final_status="$st"; final_msg="$msg"; break ;;
            esac
        fi
        sleep 2
    done
fi

[ "$final_status" = "submitted" ] && final_msg="已提交，尚未确认完成（可用 --status 查看）"

case "$final_status" in
    completed) echo "✅ 打印完成：$final_msg"; rc=0 ;;
    submitted) echo "⏳ $final_msg"; rc=0 ;;
    *) echo "❌ 打印未成功（$final_status）：$final_msg"; rc=6 ;;
esac

if [ "$AS_JSON" = 1 ]; then
    python3 - "$job_id" "${cups_id:-}" "$PRINTER" "$final_status" "$final_msg" "$remote_name" <<'PY'
import json,sys
jid,cups,printer,status,msg,name=sys.argv[1:7]
print(json.dumps({"job_id":jid,"cups_job_id":cups,"printer":printer,
 "status":status,"message":msg,"filename":name},ensure_ascii=False))
PY
fi
exit $rc
