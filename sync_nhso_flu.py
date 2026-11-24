# -*- coding: utf-8 -*-
"""
ระบบดึงข้อมูลวัคซีนไข้หวัดใหญ่จาก สปสช. (medata) เพื่ออัพเดท Fludashboard.html อัตโนมัติทุกวัน
================================================================
แหล่งข้อมูล: แดชบอร์ดสาธารณะของ สปสช. https://medata.nhso.go.th/me/public/dashboard/41/881
ซึ่งเป็น Tableau embed บน https://bicloud.nhso.go.th เปิดให้ guest เข้าดูได้โดยไม่ต้อง login

วิธีดึง (POST ล้วน, ไม่ต้องใช้เบราว์เซอร์/JWT/คุกกี้ผู้ใช้):
  1. startSession/viewing  -> sessionid ของ guest (is_guest=true) + sheetId
  2. bootstrapSession      -> ผูก session กับชีต
  3. ensure-layout-for-sheet + api-get-worksheet-summary-logical-table-data
     ของชีต "rpt" ใน "Dashboard รายงานบริการ" -> ตารางรายหน่วยบริการทั้งประเทศ
     แล้วกรองเฉพาะ 18 เครือข่ายของกาฬสินธุ์ (เขต 7)
  4. คำสั่งเดียวกันกับชีต "Sheet 20" ในแดชบอร์ดภาพรวม -> เวลา LAST_UPDATED

ความหมายของคอลัมน์ (ตรวจกับข้อมูลจริงแล้ว):
  - เป้าหมายเครือข่าย = แถวหน่วยแม่ข่าย (รหัสหน่วย == รหัสเครือข่าย) คอลัมน์ "เป้าหมาย(ทั้งหมด)"
  - เป้าหมายรายหน่วย   = คอลัมน์ "เป้าหมาย(รายหน่วย)" ของแต่ละหน่วย
  - ฉีดแล้วของเครือข่าย = ผลรวมคอลัมน์ "ฉีด" ของทุกหน่วยในเครือข่าย
  - V11/V12 แยกจากคำนำหน้าของคอลัมน์ "รายาการเบิก"

วิธีใช้:
  1. รัน: python sync_nhso_flu.py
  2. ทดสอบโดยไม่ push: FLU_NO_PUSH=1 python sync_nhso_flu.py

หมายเหตุ TLS: bicloud.nhso.go.th เสิร์ฟ chain ครบ (GlobalSign) จึงใช้ ssl แบบตรวจใบรับรองตามปกติ
"""

import os
import re
import sys
import io
import json
import gzip
import ssl
import urllib.request
import urllib.error
import urllib.parse
import http.cookiejar
import subprocess
from datetime import datetime, timezone, timedelta

# Set output encoding to UTF-8 to prevent encoding errors on Windows
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

BANGKOK = timezone(timedelta(hours=7))
BASE = "https://bicloud.nhso.go.th"
VQ = BASE + "/vizql/w/v2/v/Dashboard"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
DASH_RPT = "Dashboard รายงานบริการ"
DASH_OV = "Dashboard รายงานภาพรวมการดำเนินงานวัคซีนไข้หวัดใหญ่"
# 18 รหัสเครือข่ายของกาฬสินธุ์ (เขต 7) — ตรงกับชุดเครือข่ายในแดชบอร์ด
KALASIN_NC = {"10709", "11077", "11078", "11079", "11080", "11081", "11082", "11083",
              "11084", "11085", "11086", "11087", "11088", "11449", "28017", "28789",
              "28790", "28791"}
THAI_MONTHS = ["", "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
               "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]
NULLS = (None, "", "%null%", "Null")


def find_git_exe():
    import shutil
    git_path = shutil.which('git')
    if git_path:
        return git_path
    standard_paths = [
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files (x86)\Git\cmd\git.exe",
    ]
    userprofile = os.environ.get('USERPROFILE', '')
    if userprofile:
        github_desktop_dir = os.path.join(userprofile, r"AppData\Local\GitHubDesktop")
        if os.path.exists(github_desktop_dir):
            for root, dirs, files in os.walk(github_desktop_dir):
                if 'git.exe' in files:
                    return os.path.join(root, 'git.exe')
    for p in standard_paths:
        if os.path.exists(p):
            return p
    return "git"


def find_repo_dir(repo_name):
    if os.path.isdir(os.path.join(SCRIPT_DIR, '.git')):
        return SCRIPT_DIR
    parent_dir = os.path.dirname(SCRIPT_DIR)
    if os.path.isdir(os.path.join(parent_dir, '.git')):
        return parent_dir
    paths_to_check = [os.path.join(r"C:\GitRepos", repo_name)]
    userprofile = os.environ.get('USERPROFILE', '')
    if userprofile:
        paths_to_check.extend([
            os.path.join(userprofile, "OneDrive", "เอกสาร", "GitHub", repo_name),
            os.path.join(userprofile, "OneDrive", "Documents", "GitHub", repo_name),
            os.path.join(userprofile, "Documents", "GitHub", repo_name),
            os.path.join(userprofile, repo_name),
        ])
    for p in paths_to_check:
        if os.path.exists(p) and os.path.isdir(os.path.join(p, '.git')):
            return p
    return None


GIT_EXE = find_git_exe()
GIT_DIR = find_repo_dir('MMR2')
if not GIT_DIR:
    print("❌ ไม่พบโฟลเดอร์ Git Repository ของ MMR2")
    sys.exit(1)

HTML_PATH = os.path.join(GIT_DIR, 'Fludashboard.html')
PUSH_TO_GITHUB = os.environ.get('FLU_NO_PUSH') != '1'

QS = ("%3Aembed=y&%3AapiID=embhost0&%3AapiInternalVersion=1.162.0&%3AapiExternalVersion=3.10.0"
      "&navType=0&navSrc=Opt&%3AdisableUrlActionsPopups=n&%3Atabs=y&%3Atoolbar=top&%3Adevice=default"
      "&mobile=n&%3AhideEditButton=n&%3AhideEditInDesktopButton=n&%3AsuppressDefaultEditBehavior=n"
      "&%3Ajsdebug=n&%3Asubscriptions=no&%3AshowAskData=false&%3AshowDataDetails=no"
      "&%3AshowAppBanner=false&%3AshowVizHome=no&%3Aredirect=auth")


def num(value):
    if value in NULLS:
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def fetch_nhso_data():
    """คืน (asof_raw, kalasin_rows, columns) หรือ (None, None, None) เมื่อเรียกไม่สำเร็จ"""
    print("🌐 กำลังดึงข้อมูลจาก สปสช. medata (Tableau guest)...")
    ctx = ssl.create_default_context()
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj),
                                     urllib.request.HTTPSHandler(context=ctx))
    op.addheaders = [("User-Agent", UA)]

    def post(url, form=None, headers=None, empty=False):
        h = {"Accept": "*/*", "X-Requested-With": "XMLHttpRequest",
             "Origin": BASE, "Referer": BASE + "/views/v2/Dashboard"}
        h.update(headers or {})
        data = b"" if empty else urllib.parse.urlencode(form or {}).encode()
        if not empty:
            h["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        r = urllib.request.Request(url, data=data, headers=h, method="POST")
        resp = op.open(r, timeout=180)
        b = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            b = gzip.GzipFile(fileobj=io.BytesIO(b)).read()
        return b

    def summary(sid, worksheet, dashboard):
        post(VQ + "/sessions/" + sid + "/commands/tabsrv/ensure-layout-for-sheet",
             {"target-sheet": dashboard}, {"X-Tsi-Session-Id": sid})
        form = {"max-rows": "0", "ignore-aliases": "false", "ignore-selection": "true",
                "visual-id-pres-model": json.dumps({"worksheet": worksheet, "dashboard": dashboard},
                                                   ensure_ascii=False),
                "version-name": "1.0"}
        b = post(VQ + "/sessions/" + sid +
                 "/commands/tabdoc/api-get-worksheet-summary-logical-table-data", form,
                 {"X-Tsi-Session-Id": sid})
        j = json.loads(b)
        cr = j["vqlCmdResponse"]["cmdResultList"][0]["commandReturn"]
        if "dataTablePresModel" not in cr:
            raise ValueError("command failed: " + json.dumps(cr, ensure_ascii=False)[:300])
        dt = cr["dataTablePresModel"]
        cap = {c["uniqueName"]: c["fieldCaption"] for c in dt["showDataTableColumnPresModels"]}
        tbl = json.loads(dt["showDataFormattedTable"])["table"]
        cols = [cap.get(s, s) for s in tbl["schema"]]
        return cols, tbl["tuples"]

    try:
        j = json.loads(post(VQ + "/startSession/viewing?" + QS, empty=True,
                            headers={"Accept": "application/json"}))
        sid = j["sessionid"]
        if not j.get("is_guest"):
            raise ValueError("guest access disabled (is_guest != true)")
        post(VQ + "/bootstrapSession/sessions/" + sid, {"sheet_id": j["sheetId"]},
             {"X-Tsi-Session-Id": sid})
        cols, rows = summary(sid, "rpt", DASH_RPT)
        _, upd = summary(sid, "Sheet 20", DASH_OV)
        asof_raw = upd[0][0]
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError,
            IndexError, OSError) as e:
        print(f"❌ เรียก สปสช. medata ไม่สำเร็จ: {e}")
        return None, None, None

    idx = {c: i for i, c in enumerate(cols)}
    need = ["เครือข่าย", "รหัสหน่วยบริการ", "ชื่อหน่วยบริการ", "ประเภท",
            "รายาการเบิก", "ชื่อการวัดผล", "ค่าการวัดผล"]
    missing = [c for c in need if c not in idx]
    if missing:
        print(f"❌ คอลัมน์ที่ต้องการหายจาก API: {missing}")
        return None, None, None
    kal = [r for r in rows if r[idx["เครือข่าย"]] in KALASIN_NC]
    if not kal or not asof_raw:
        print("❌ API ไม่ส่งแถวของกาฬสินธุ์ หรือไม่มีเวลา LAST_UPDATED")
        return None, None, None
    print(f"✅ ดึงข้อมูลสำเร็จ: ทั้งประเทศ {len(rows)} แถว | กาฬสินธุ์ {len(kal)} แถว "
          f"| {len({r[idx['รหัสหน่วยบริการ']] for r in kal})} หน่วยบริการ | LAST_UPDATED {asof_raw}")
    return asof_raw, kal, cols


def parse_asof(asof_raw):
    """'19/11/2569 06:26:00' -> (datecom ไทย, asof สั้น ค.ศ.)"""
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4}) (\d{2}:\d{2})", asof_raw.strip())
    if not m:
        raise ValueError(f"รูปแบบ LAST_UPDATED ไม่รู้จัก: {asof_raw!r}")
    d, mo, y, hm = int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4)
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        raise ValueError(f"วันที่ไม่สมเหตุสมผล: {asof_raw!r}")
    datecom = f"{d} {THAI_MONTHS[mo]} {y} {hm}"
    asof = f"{d:02d}/{mo:02d}/{y - 543} {hm}"
    return datecom, asof


FLU_BLOCK_RE = re.compile(r'const FLU_DATA = \{[\s\S]*?\n\};')


def parse_existing(html_content):
    block = FLU_BLOCK_RE.search(html_content)
    if not block:
        print("❌ ไม่พบบล็อก FLU_DATA ใน Fludashboard.html")
        sys.exit(1)
    m = re.search(r'rows:\s*`([\s\S]*?)`', block.group(0))
    if not m:
        print("❌ บล็อก FLU_DATA ไม่มีฟิลด์ rows")
        sys.exit(1)
    nets = json.loads(m.group(1))
    print(f"✅ อ่านข้อมูลเดิมสำเร็จ ({len(nets)} เครือข่าย, "
          f"{sum(len(n['units']) for n in nets)} หน่วยบริการ)")
    return nets


def build_networks(kal, cols, old_nets):
    """สร้าง ND ใหม่โดยคงลำดับอำเภอ/เครือข่าย/หน่วยเดิมไว้ แล้วเติมค่าจาก API"""
    idx = {c: i for i, c in enumerate(cols)}
    units = {}
    mains = {}
    for r in kal:
        nc = r[idx["เครือข่าย"]]
        code = r[idx["รหัสหน่วยบริการ"]]
        item = r[idx["รายาการเบิก"]] or ""
        v = "v11" if item.startswith("V11") else "v12" if item.startswith("V12") else None
        if v is None:
            continue
        measure = r[idx["ชื่อการวัดผล"]]
        val = num(r[idx["ค่าการวัดผล"]]) or 0.0
        u = units.setdefault((nc, code), {"name": (r[idx["ชื่อหน่วยบริการ"]] or "").strip(),
                                          "type": (r[idx["ประเภท"]] or "").strip(),
                                          "v11t": 0.0, "v11v": 0.0, "v12t": 0.0, "v12v": 0.0})
        if measure == "ฉีด":
            u[v + "v"] += val
        elif measure == "เป้าหมาย(รายหน่วย)":
            u[v + "t"] += val
        elif measure == "เป้าหมาย(ทั้งหมด)" and code == nc:
            mains.setdefault(nc, {"v11t": 0.0, "v12t": 0.0})
            mains[nc][v + "t"] += val

    problems = []
    old_nc = {n["nc"] for n in old_nets}
    new_nc = {nc for nc, _ in units}
    if new_nc - old_nc:
        problems.append(f"พบเครือข่ายใหม่ที่ไม่มีอำเภอในข้อมูลเดิม: {sorted(new_nc - old_nc)}")
    if old_nc - new_nc:
        problems.append(f"เครือข่ายเดิมหายจาก API: {sorted(old_nc - new_nc)}")
    for nc in sorted(old_nc & new_nc):
        if nc not in mains:
            problems.append(f"เครือข่าย {nc} ไม่มีแถวแม่ข่าย (เป้าหมายทั้งหมด)")

    built = []
    for old in old_nets:
        nc = old["nc"]
        if nc not in new_nc or nc not in mains:
            continue
        main_name = units[(nc, nc)]["name"]
        n = {"nc": nc, "district": old["district"], "mn": main_name or old["mn"],
             "sc": 0, "v11t": mains[nc]["v11t"], "v11v": 0.0,
             "v12t": mains[nc]["v12t"], "v12v": 0.0, "tt": 0.0, "tv": 0.0, "pct": 0,
             "units": []}
        seen = set()
        ordered = [(u["code"], u) for u in old["units"]]
        old_codes = {c for c, _ in ordered}
        for (unc, code), u in sorted(units.items()):
            if unc == nc and code not in old_codes:
                ordered.append((code, u))
        for code, u in ordered:
            if (nc, code) not in units:
                problems.append(f"หน่วย {code} ({nc}) หายจาก API")
                continue
            a = units[(nc, code)]
            seen.add(code)
            tt = a["v11t"] + a["v12t"]
            tv = a["v11v"] + a["v12v"]
            n["units"].append({"code": code, "name": a["name"], "type": a["type"],
                               "v11t": a["v11t"], "v11v": a["v11v"],
                               "v12t": a["v12t"], "v12v": a["v12v"],
                               "tt": tt, "tv": tv,
                               "pct": round(tv / tt * 100, 1) if tt else 0})
            if a["type"] == "ลูกข่าย":
                n["sc"] += 1
            n["v11v"] += a["v11v"]
            n["v12v"] += a["v12v"]
        n["tt"] = n["v11t"] + n["v12t"]
        n["tv"] = n["v11v"] + n["v12v"]
        n["pct"] = round(n["tv"] / n["tt"] * 100, 1) if n["tt"] else 0
        built.append(n)
    return built, problems


def verify_networks(built, kal, cols):
    """ตรวจยอดที่สร้างใหม่กับผลรวมตรงจากแถว API (อิสระจากการสร้าง) — ว่าง = ผ่าน"""
    idx = {c: i for i, c in enumerate(cols)}
    exp = {"v11t": 0.0, "v11v": 0.0, "v12t": 0.0, "v12v": 0.0}
    exp_unit = {"v11t": 0.0, "v12t": 0.0}
    pairs = set()
    for r in kal:
        nc = r[idx["เครือข่าย"]]
        code = r[idx["รหัสหน่วยบริการ"]]
        pairs.add((nc, code))
        item = r[idx["รายาการเบิก"]] or ""
        v = "v11" if item.startswith("V11") else "v12" if item.startswith("V12") else None
        if v is None:
            continue
        val = num(r[idx["ค่าการวัดผล"]]) or 0.0
        measure = r[idx["ชื่อการวัดผล"]]
        if measure == "ฉีด":
            exp[v + "v"] += val
        elif measure == "เป้าหมาย(ทั้งหมด)" and code == nc:
            exp[v + "t"] += val
        elif measure == "เป้าหมาย(รายหน่วย)":
            exp_unit[v + "t"] += val

    act = {"v11t": 0.0, "v11v": 0.0, "v12t": 0.0, "v12v": 0.0}
    act_unit = {"v11t": 0.0, "v12t": 0.0}
    got_pairs = set()
    problems = []
    for n in built:
        for k in act:
            act[k] += n[k]
        for u in n["units"]:
            got_pairs.add((n["nc"], u["code"]))
            act_unit["v11t"] += u["v11t"]
            act_unit["v12t"] += u["v12t"]
            if abs(u["tt"] - (u["v11t"] + u["v12t"])) > 0.01:
                problems.append(f"หน่วย {u['code']}: tt ไม่เท่า v11t+v12t")
            if abs(u["tv"] - (u["v11v"] + u["v12v"])) > 0.01:
                problems.append(f"หน่วย {u['code']}: tv ไม่เท่า v11v+v12v")
    if got_pairs != pairs:
        problems.append(f"ชุด (เครือข่าย,หน่วย) ไม่ตรง API: หาย {sorted(pairs - got_pairs)} "
                        f"เกิน {sorted(got_pairs - pairs)}")
    for k in exp:
        if abs(act[k] - exp[k]) > 0.01:
            problems.append(f"ยอดรวมเครือข่าย {k}: API={exp[k]:.0f} แต่สร้างได้={act[k]:.0f}")
    for k in exp_unit:
        if abs(act_unit[k] - exp_unit[k]) > 0.01:
            problems.append(f"ยอดรวมรายหน่วย {k}: API={exp_unit[k]:.0f} แต่สร้างได้={act_unit[k]:.0f}")
    return problems


def escape_template(text):
    return text.replace('\\', '\\\\').replace('`', '\\`').replace('${', '\\${')


def render_flu_block(datecom, asof, built):
    rows_js = escape_template(json.dumps(built, ensure_ascii=False, separators=(",", ":")))
    return ('const FLU_DATA = {\n'
            f'  datecom: {json.dumps(datecom, ensure_ascii=False)},\n'
            f'  asof: {json.dumps(asof, ensure_ascii=False)},\n'
            f'  rows: `{rows_js}`\n'
            '};')


def update_html(new_block):
    print("📝 แก้ไขไฟล์ Fludashboard.html...")
    with open(HTML_PATH, 'r', encoding='utf-8', newline='') as f:
        html_content = f.read()
    if '\r\n' in html_content:
        new_block = new_block.replace('\n', '\r\n')
    if not FLU_BLOCK_RE.search(html_content):
        print("❌ ไม่พบตำแหน่งสำหรับเขียนบล็อกข้อมูล")
        sys.exit(1)
    new_html = FLU_BLOCK_RE.sub(lambda m: new_block, html_content, count=1)
    with open(HTML_PATH, 'w', encoding='utf-8', newline='') as f:
        f.write(new_html)
    print("✅ เขียนไฟล์สำเร็จ!")


def run_git(datecom):
    """คืน True เมื่อ push สำเร็จหรือไม่มีอะไรต้อง commit, False เมื่อล้มเหลว (ผู้เรียกต้อง exit 1)"""
    if not PUSH_TO_GITHUB:
        print("⏭️  ข้ามการ push ขึ้น GitHub (ตั้งค่า FLU_NO_PUSH=1)")
        return True
    if not os.path.exists(GIT_EXE):
        print(f"❌ ไม่พบ git.exe ที่ {GIT_EXE}")
        return False
    print(f"🚀 กำลังส่งออกข้อมูลขึ้น GitHub ที่: {GIT_DIR}")
    try:
        subprocess.run([GIT_EXE, 'add', 'Fludashboard.html'], cwd=GIT_DIR, check=True,
                       capture_output=True, text=True)
        staged = subprocess.run([GIT_EXE, 'diff', '--cached', '--quiet'], cwd=GIT_DIR)
        if staged.returncode == 0:
            print("ℹ️  ไม่มีการเปลี่ยนแปลงของข้อมูล จึงไม่มี commit ใหม่")
            return True
        if staged.returncode != 1:
            print(f"❌ git diff --cached ล้มเหลว (exit {staged.returncode})")
            return False
        commit_msg = f"Auto-update Flu from NHSO medata (ข้อมูล ณ วันที่ {datecom})"
        subprocess.run([GIT_EXE, 'commit', '-m', commit_msg], cwd=GIT_DIR, check=True,
                       capture_output=True, text=True)
        subprocess.run([GIT_EXE, 'pull', '--rebase', 'origin', 'main'], cwd=GIT_DIR, check=True,
                       capture_output=True, text=True)
        subprocess.run([GIT_EXE, 'push', 'origin', 'main'], cwd=GIT_DIR, check=True,
                       capture_output=True, text=True)
        print("🎉 ดันข้อมูลขึ้น GitHub Pages เรียบร้อยแล้ว!")
        print("🔗 https://zole3500-ana.github.io/MMR2/Fludashboard.html")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ git {e.cmd[1]} ล้มเหลว (exit {e.returncode})")
        detail = (e.stderr or e.stdout or '').strip()
        if detail:
            print(f"   {detail}")
        return False


def main():
    print("=" * 60)
    print("  ระบบดึงข้อมูล สปสช. medata อัตโนมัติและอัพเดท Flu แดชบอร์ด")
    print("=" * 60)

    asof_raw, kal, cols = fetch_nhso_data()
    if kal is None:
        print("❌ เรียกแหล่งข้อมูลไม่สำเร็จ — คงข้อมูลเดิมไว้ ไม่เขียนไฟล์")
        sys.exit(1)

    try:
        datecom, asof = parse_asof(asof_raw)
    except ValueError as e:
        print(f"❌ {e} — คงข้อมูลเดิมไว้")
        sys.exit(1)

    with open(HTML_PATH, 'r', encoding='utf-8', newline='') as f:
        html_content = f.read()
    old_nets = parse_existing(html_content)

    built, problems = build_networks(kal, cols, old_nets)
    if problems:
        print("❌ โครงสร้างเครือข่าย/หน่วยไม่ตรงเดิม — คงข้อมูลเดิมไว้")
        for p in problems:
            print(f"   - {p}")
        sys.exit(1)

    problems = verify_networks(built, kal, cols)
    if problems:
        print("❌ cross-check ไม่ผ่าน — คงข้อมูลเดิมไว้ ไม่เผยแพร่ตัวเลขที่รู้ว่าผิด")
        for p in problems:
            print(f"   - {p}")
        sys.exit(1)
    print(f"🔎 cross-check ผ่าน ({len(built)} เครือข่าย, "
          f"{sum(len(n['units']) for n in built)} หน่วยบริการ, ยอดรวมตรง API)")

    update_html(render_flu_block(datecom, asof, built))
    git_ok = run_git(datecom)

    tv11 = sum(n["v11v"] for n in built)
    tt11 = sum(n["v11t"] for n in built)
    tv12 = sum(n["v12v"] for n in built)
    tt12 = sum(n["v12t"] for n in built)
    print("=" * 60)
    print(f"  ข้อมูล ณ วันที่ {datecom} น. (asof {asof})")
    print(f"  V11: ฉีด {tv11:,.0f} / เป้า {tt11:,.0f} | V12: ฉีด {tv12:,.0f} / เป้า {tt12:,.0f}")
    if not git_ok:
        print("  ❌ ส่งข้อมูลขึ้น GitHub ไม่สำเร็จ")
        print("  จบแบบมีข้อผิดพลาด — exit 1 (GitHub Actions จะขึ้นแดงและแจ้งเตือน)")
        print("=" * 60)
        sys.exit(1)
    print("  ดำเนินการสำเร็จเรียบร้อย!")
    print("=" * 60)


if __name__ == '__main__':
    main()