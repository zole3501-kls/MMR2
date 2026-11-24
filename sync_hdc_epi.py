# -*- coding: utf-8 -*-
"""
ระบบดึงข้อมูลจาก HDC API เพื่ออัพเดทแดชบอร์ด EPI ปกติ (เข็มที่เหลือจาก MMR2 และ PCV) อัตโนมัติ
================================================================
ดึงสองรายงานต่อปีงบประมาณ แล้วรวมเป็นแถวเดียวต่อหน่วยบริการ:
  รายงาน 1.1 (เด็กอายุครบ 1 ปี)  EXCHANGE_ID c88658f4d09acb8c76e6c06ffb58ebaf  -> 17 เข็ม
  รายงาน 2.1 (เด็กอายุครบ 2 ปี)  EXCHANGE_ID 71e49e4b61aafee17761601c506d5f85  -> 5 เข็ม
ไม่รวม MMR2 (รายงาน 2.1 มีแดชบอร์ดแล้ว) และ PCV 2/4 เดือน (รายงาน 1.1 มีแดชบอร์ดแล้ว)
และไม่นับ PCV 6 เดือน เพราะ HDC ระบุเป็น "นอกแผน"

ชื่อไทยของทุกเข็มยืนยันจากหัวตารางใน UI ของ HDC แล้ว (อย่าเดาจากชื่อคีย์):
  กับดัก: hbv4 = HBV 4 เดือน แต่ hbv3 = HBV 6 เดือน · hbv (alias hbv1) = HBV แรกเกิด
          ipv2 ใช้คีย์รายไตรมาสเป็น ipvq1..q4 (ไม่มี ipv2q1) · mmr1 ในรายงาน 2.1 = "MMR1 เก็บตก"
          ไม่ใช่ MMR 9 เดือน (ซึ่งคือ mmr ในรายงาน 1.1)

วิธีใช้:
  1. รัน: uv run sync_hdc_epi.py
  2. ทดสอบโดยไม่ push: EPI_NO_PUSH=1 uv run sync_hdc_epi.py

หมายเหตุ: ปีงบประมาณไทยเริ่ม 1 ตุลาคม ดังนั้น ปีงบประมาณ = ปี ค.ศ. + 543 (+1 ถ้าเดือน >= 10)
"""

import os
import re
import sys
import io
import json
import urllib.request
import subprocess
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from hdc_http import fetch_json_with_retry

# Set output encoding to UTF-8 to prevent encoding errors on Windows
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

BANGKOK = timezone(timedelta(hours=7))
EXCHANGE_IDS = {
    'r11': "c88658f4d09acb8c76e6c06ffb58ebaf",
    'r21': "71e49e4b61aafee17761601c506d5f85",
}
QUARTERS = (1, 2, 3, 4)

# (คีย์ยอดทั้งปี, คีย์นำหน้ารายไตรมาส, ชื่อไทยจากหัวตาราง HDC) เรียงตามแผนงาน
R11_INDICATORS = (
    ('bcg', 'bcg', 'BCG แรกเกิด'),
    ('hbv', 'hbv', 'HBV แรกเกิด'),
    ('dtp1', 'dtp1', 'DTP 2 เดือน'),
    ('dtp2', 'dtp2', 'DTP 4 เดือน'),
    ('dtp3', 'dtp3', 'DTP 6 เดือน'),
    ('hbv2', 'hbv2', 'HBV 2 เดือน'),
    ('hbv4', 'hbv4', 'HBV 4 เดือน'),
    ('hbv3', 'hbv3', 'HBV 6 เดือน'),
    ('hib1', 'hib1', 'Hib 2 เดือน'),
    ('hib2', 'hib2', 'Hib 4 เดือน'),
    ('hib3', 'hib3', 'Hib 6 เดือน'),
    ('ipv1', 'ipv1', 'IPV 2 เดือน'),
    ('ipv2', 'ipv', 'IPV 4 เดือน'),
    ('opv3', 'opv3', 'OPV 6 เดือน'),
    ('mmr', 'mmr', 'MMR 9 เดือน'),
    ('rota1', 'rota1', 'Rota 2 เดือน'),
    ('rota', 'rota', 'Rota (ครบถ้วนตามเกณฑ์)'),
)
R21_INDICATORS = (
    ('dtp4', 'dtp4', 'DTP 1 ปี 6 เดือน'),
    ('opv4', 'opv4', 'OPV 1 ปี 6 เดือน'),
    ('je2', 'je2', 'LAJE/JE 1 ปี'),
    ('pcv4', 'pcv4', 'PCV 1 ปี'),
    ('mmr1', 'mmr1', 'MMR1 เก็บตก'),
)
# หนึ่งช่วงไตรมาสในแถวข้อมูล: [เป้า 1.1] + [17 เข็ม 1.1] + [เป้า 2.1] + [5 เข็ม 2.1]
QBLOCK = 1 + len(R11_INDICATORS) + 1 + len(R21_INDICATORS)
NCOLS = 4 + 4 * QBLOCK

EPI_FIRST_YEAR = 2568
HISTORY_YEARS = 3


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

HTML_PATH = os.path.join(GIT_DIR, 'EPIdashboard.html')
MMR2_HTML_PATH = os.path.join(GIT_DIR, 'dashboard.html')
PUSH_TO_GITHUB = os.environ.get('EPI_NO_PUSH') != '1'


def current_fiscal_year():
    now = datetime.now(BANGKOK)
    return now.year + 543 + (1 if now.month >= 10 else 0)


def num(value):
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fmt_num(value):
    if value is None:
        return ''
    return str(int(value)) if value == int(value) else str(value)


def fetch_one(report, year):
    """คืน (datecom, inner_data, reason) สำหรับรายงานเดียว"""
    url = (
        f"https://api-hdc.moph.go.th/v1/reports/province/data/{EXCHANGE_IDS[report]}"
        f"?table_display=provider&year={year}&month=ALL&zone=07&province_code=46"
        "&district_code=ALL&subdistrict_code=ALL&department_code=ALL&organization_type=ALL"
        "&ministry=ALL&hospital=ALL&service_plan=ALL&jurisdiction_code=ALL"
        "&freeze_month=ALL&mental_code=ALL&mental_group_code=ALL&custom=[]"
    )
    data_json, err = fetch_json_with_retry(url, label=f"EPI-{report}-{year}", timeout=30)
    if err or not data_json:
        print(f"❌ เรียก HDC API รายงาน {report} ปี {year} ไม่สำเร็จ: {err}")
        return None, None, 'error'

    if not data_json.get('ok'):
        print(f"❌ HDC API รายงาน {report} ปี {year} ตอบกลับ ok: false")
        return None, None, 'error'

    rows = data_json.get('rows') or []
    if not rows:
        return '', [], 'not_published'
    datecom = rows[0].get('datecom')
    inner = rows[0].get('data') or []
    if not datecom or not inner:
        return '', [], 'not_published'
    return datecom, inner, None


def has_any_value(item, indicators):
    return any(num(item.get(f'{qkey}q{q}')) is not None
               for _, qkey, _ in indicators for q in QUARTERS) \
        or any(num(item.get(f'targetq{q}')) is not None for q in QUARTERS)


def fetch_hdc_data(year):
    """คืน (datecom, {'r11': [...], 'r21': [...]}, reason)
    - reason 'error'         = รายงานใดรายงานหนึ่งเรียกไม่สำเร็จ (อย่าแตะข้อมูลเดิม)
    - reason 'not_published' = ว่างทั้งสองรายงาน
    - reason None            = มีข้อมูลอย่างน้อยหนึ่งรายงาน (อีกรายงานอาจว่าง -> ช่องว่างในแถว)
    """
    print(f"🌐 กำลังดึงข้อมูล EPI จาก HDC API ปีงบประมาณ {year} (รายงาน 1.1 + 2.1 พร้อมกัน)...")
    got = {}
    reasons = {}
    datecoms = {}
    
    # Run fetch_one in parallel for r11 and r21
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_r11 = executor.submit(fetch_one, 'r11', year)
        future_r21 = executor.submit(fetch_one, 'r21', year)
        res_r11 = future_r11.result()
        res_r21 = future_r21.result()

    for report, indicators, (datecom, inner, reason) in [
        ('r11', R11_INDICATORS, res_r11),
        ('r21', R21_INDICATORS, res_r21)
    ]:
        reasons[report] = reason
        if reason == 'error':
            return None, None, 'error'
        if reason == 'not_published':
            got[report] = []
            continue
        usable = [it for it in inner if (it.get('a_code') or '').strip() and has_any_value(it, indicators)]
        print(f"✅ ปี {year} รายงาน {report}: ณ วันที่ {datecom} · {len(usable)} หน่วยที่มีข้อมูล (API ส่งมา {len(inner)})")
        got[report] = usable
        datecoms[report] = datecom

    if not got['r11'] and not got['r21']:
        print(f"⚠️  ปีงบประมาณ {year} ยังไม่มีข้อมูลในระบบ HDC (ว่างทั้งสองรายงาน)")
        return '', {'r11': [], 'r21': []}, 'not_published'
    if not got['r11'] or not got['r21']:
        empty = 'r11' if not got['r11'] else 'r21'
        print(f"⚠️  ปี {year}: รายงาน {empty} ว่างแต่อีกรายงานมีข้อมูล — เข็มของรายงานที่ว่างจะปล่อยเป็นช่องว่าง")
    datecom = datecoms.get('r11') or datecoms.get('r21')
    return datecom, got, None


EPI_BLOCK_RE = re.compile(r'const EPI_DATA = \{[\s\S]*?\n\};')
MMR2_BLOCK_RE = re.compile(r'const HDC_DATA = \{[\s\S]*?\n\};')
YEAR_ENTRY_RE = re.compile(
    r'"(\d{4})":\s*\{\s*datecom:\s*(null|"(?:[^"\\]|\\.)*"),\s*rows:\s*`([\s\S]*?)`'
    r'(?:,\s*note:\s*(null|"(?:[^"\\]|\\.)*"))?\s*\}'
)


def parse_row_metadata(line, metadata_map):
    parts = line.split(',')
    if len(parts) < 4:
        return
    try:
        idx = int(parts[0])
    except ValueError:
        idx = 999
    district = parts[1].strip()
    unit_full = parts[2].strip()
    agency = parts[3].strip()
    hospcode = unit_full.split(':')[0].strip() if ':' in unit_full else unit_full
    if hospcode and hospcode not in metadata_map:
        metadata_map[hospcode] = (idx, district, unit_full, agency)


def parse_existing(html_content):
    metadata_map = {}
    years = {}
    block = EPI_BLOCK_RE.search(html_content)
    if not block:
        print("❌ ไม่พบบล็อกข้อมูล EPI_DATA ในไฟล์ HTML")
        sys.exit(1)
    for m in YEAR_ENTRY_RE.finditer(block.group(0)):
        year, datecom_raw, rows, note_raw = m.group(1), m.group(2), m.group(3), m.group(4)
        datecom = None if datecom_raw == 'null' else json.loads(datecom_raw)
        note = None if not note_raw or note_raw == 'null' else json.loads(note_raw)
        years[year] = {'datecom': datecom, 'rows': rows.strip(), 'note': note}
    for year in sorted(years, reverse=True):
        for line in years[year]['rows'].split('\n'):
            if line.strip():
                parse_row_metadata(line.strip(), metadata_map)
    print(f"✅ อ่านข้อมูลเดิมสำเร็จ ({len(years)} ปี, {len(metadata_map)} หน่วยบริการ)")
    return metadata_map, years


def load_mmr2_metadata():
    """ยืมอำเภอ/สังกัด/idx จากแดชบอร์ด MMR2 เพราะ API ไม่ส่งข้อมูลพื้นที่มาด้วย"""
    metadata_map = {}
    if not os.path.exists(MMR2_HTML_PATH):
        print("⚠️  ไม่พบ dashboard.html — หน่วยบริการใหม่จะไม่มีอำเภอ/สังกัด")
        return metadata_map
    with open(MMR2_HTML_PATH, 'r', encoding='utf-8') as f:
        content = f.read()
    block = MMR2_BLOCK_RE.search(content)
    if not block:
        print("⚠️  ไม่พบบล็อก HDC_DATA ใน dashboard.html — ข้ามการยืมเมตาดาต้า")
        return metadata_map
    rows_by_year = {m.group(1): m.group(3).strip() for m in YEAR_ENTRY_RE.finditer(block.group(0))}
    for year in sorted(rows_by_year, reverse=True):
        for line in rows_by_year[year].split('\n'):
            if line.strip():
                parse_row_metadata(line.strip(), metadata_map)
    print(f"✅ ยืมเมตาดาต้าหน่วยบริการจากแดชบอร์ด MMR2 ({len(metadata_map)} หน่วย)")
    return metadata_map


def generate_year_rows(got, metadata_map):
    """สร้างแถว NCOLS คอลัมน์:
    idx,district,code:name,agency แล้วต่อไตรมาสละ [เป้า1.1, 17 เข็ม1.1, เป้า2.1, 5 เข็ม2.1]"""
    by_code = {}
    for report in ('r11', 'r21'):
        for item in got[report]:
            code = (item.get('a_code') or '').strip()
            if code:
                by_code.setdefault(code, {})[report] = item

    rows_data = []
    for code, reports in by_code.items():
        values = []
        for q in QUARTERS:
            i11 = reports.get('r11') or {}
            i21 = reports.get('r21') or {}
            values.append(num(i11.get(f'targetq{q}')))
            for _, qkey, _ in R11_INDICATORS:
                values.append(num(i11.get(f'{qkey}q{q}')))
            values.append(num(i21.get(f'targetq{q}')))
            for _, qkey, _ in R21_INDICATORS:
                values.append(num(i21.get(f'{qkey}q{q}')))

        if code in metadata_map:
            idx, district, unit_full, agency = metadata_map[code]
        else:
            idx = 999
            district = ""
            src = reports.get('r11') or reports.get('r21') or {}
            a_name = (src.get('a_name') or '').strip()
            unit_name_clean = a_name.split(':')[1].strip() if ':' in a_name else a_name
            unit_full = f"{code}:{unit_name_clean}"
            agency = "สำนักงานสาธารณสุขจังหวัด"
            metadata_map[code] = (idx, district, unit_full, agency)

        cells = [str(idx), district, unit_full, agency] + [fmt_num(v) for v in values]
        rows_data.append((idx, ','.join(cells)))

    rows_data.sort(key=lambda x: x[0])
    return '\n'.join(line for _, line in rows_data)


def escape_template(text):
    return text.replace('\\', '\\\\').replace('`', '\\`').replace('${', '\\${')


def cell_index(q, slot):
    """slot: 0 = เป้า 1.1, 1..17 = เข็ม 1.1, 18 = เป้า 2.1, 19..23 = เข็ม 2.1"""
    return 4 + (q - 1) * QBLOCK + slot


def verify_year_rows(rows_text, got, year):
    """ตรวจแถวที่เพิ่งสร้างกับข้อมูลดิบจาก API ของทั้งสองรายงาน"""
    problems = []
    sources = {}
    for report, indicators in (('r11', R11_INDICATORS), ('r21', R21_INDICATORS)):
        sources[report] = [it for it in got[report]
                           if (it.get('a_code') or '').strip() and has_any_value(it, indicators)]

    expected = {}
    annual = {}
    has_annual = {}
    for report, indicators in (('r11', R11_INDICATORS), ('r21', R21_INDICATORS)):
        # คีย์ยอดทั้งปีกับคีย์รายไตรมาสไม่ตรงกันเสมอ (ipv2 -> ipvq1..q4)
        prefixes = [('target', 'target')] + [(k, qk) for k, qk, _ in indicators]
        for key, qkey in prefixes:
            exp = 0.0
            ann = 0.0
            seen = False
            for item in sources[report]:
                for q in QUARTERS:
                    v = num(item.get(f'{qkey}q{q}'))
                    if v is not None:
                        exp += v
                whole = num(item.get(key))
                if whole is not None:
                    ann += whole
                    seen = True
            expected[(report, key)] = exp
            annual[(report, key)] = ann
            has_annual[(report, key)] = seen

    actual = {k: 0.0 for k in expected}
    actual_rows = 0
    for line in rows_text.split('\n'):
        if not line.strip():
            continue
        cols = line.split(',')
        if len(cols) != NCOLS:
            problems.append(f'แถวมี {len(cols)} คอลัมน์ (ต้อง {NCOLS}): {line[:70]}')
            continue
        actual_rows += 1
        for q in QUARTERS:
            slots = [('r11', 'target', 0)]
            slots += [('r11', k, 1 + i) for i, (k, _, _) in enumerate(R11_INDICATORS)]
            slots += [('r21', 'target', 1 + len(R11_INDICATORS))]
            slots += [('r21', k, 2 + len(R11_INDICATORS) + i) for i, (k, _, _) in enumerate(R21_INDICATORS)]
            for report, key, slot in slots:
                cell = cols[cell_index(q, slot)].strip()
                if not cell:
                    continue
                v = num(cell)
                if v is None:
                    problems.append(f'คอลัมน์ {report}/{key}q{q} ไม่ใช่ตัวเลข: {cell!r}')
                else:
                    actual[(report, key)] += v

    union_codes = set()
    for report in ('r11', 'r21'):
        union_codes |= {(it.get('a_code') or '').strip() for it in sources[report]}
    union_codes.discard('')
    if actual_rows != len(union_codes):
        problems.append(f'จำนวนแถวไม่ตรง: API มี {len(union_codes)} หน่วย แต่สร้างได้ {actual_rows} แถว')

    for key, exp in expected.items():
        if abs(actual[key] - exp) > 0.01:
            problems.append(
                f'ยอดรวม {key[0]}/{key[1]} ไม่ตรงผลรวมรายไตรมาสของ API: '
                f'API={fmt_num(exp)} แต่ในแถวที่สร้าง={fmt_num(actual[key])} '
                f'(ต่าง {fmt_num(actual[key] - exp)})')
        if has_annual[key] and abs(exp - annual[key]) > 0.01:
            print(f'⚠️  ปี {year}: ผลรวมรายไตรมาสของ {key[0]}/{key[1]} ({fmt_num(exp)}) '
                  f'ไม่ตรงกับยอดทั้งปีที่ API ให้มา ({fmt_num(annual[key])}) — ตรวจสอบข้อมูลต้นทาง')

    return problems


def render_epi_block(years):
    parts = []
    for year in sorted(years, reverse=True):
        entry = years[year]
        datecom_js = 'null' if not entry['datecom'] else json.dumps(entry['datecom'], ensure_ascii=False)
        rows_js = escape_template(entry['rows'])
        note = entry.get('note')
        note_js = '' if not note else ',\n    note: ' + json.dumps(note, ensure_ascii=False)
        parts.append(f'  "{year}": {{\n    datecom: {datecom_js},\n    rows: `{rows_js}`{note_js}\n  }}')
    return 'const EPI_DATA = {\n' + ',\n'.join(parts) + '\n};'


def update_html(years):
    print("📝 แก้ไขไฟล์ EPIdashboard.html...")
    with open(HTML_PATH, 'r', encoding='utf-8') as f:
        html_content = f.read()
    new_block = render_epi_block(years)
    if not EPI_BLOCK_RE.search(html_content):
        print("❌ ไม่พบตำแหน่งสำหรับเขียนบล็อกข้อมูล")
        sys.exit(1)
    new_html = EPI_BLOCK_RE.sub(lambda m: new_block, html_content, count=1)
    with open(HTML_PATH, 'w', encoding='utf-8') as f:
        f.write(new_html)
    print("✅ เขียนไฟล์สำเร็จ!")


def run_git(years):
    if not PUSH_TO_GITHUB:
        print("⏭️  ข้ามการ push ขึ้น GitHub (ตั้งค่า EPI_NO_PUSH=1)")
        return True
    if not os.path.exists(GIT_EXE):
        print(f"❌ ไม่พบ git.exe ที่ {GIT_EXE}")
        return False
    with_data = [y for y in sorted(years, reverse=True) if years[y]['datecom']]
    latest = years[with_data[0]]['datecom'] if with_data else 'ไม่ทราบวันที่'
    year_label = '/'.join(with_data) if with_data else 'ไม่มีข้อมูล'
    print(f"🚀 กำลังส่งออกข้อมูล EPI ขึ้น GitHub ที่: {GIT_DIR}")
    try:
        subprocess.run([GIT_EXE, 'add', 'EPIdashboard.html'], cwd=GIT_DIR, check=True,
                       capture_output=True, text=True)
        staged = subprocess.run([GIT_EXE, 'diff', '--cached', '--quiet'], cwd=GIT_DIR)
        if staged.returncode == 0:
            print("ℹ️  ไม่มีการเปลี่ยนแปลงของข้อมูล จึงไม่มี commit ใหม่")
            return True
        if staged.returncode != 1:
            print(f"❌ git diff --cached ล้มเหลว (exit {staged.returncode})")
            return False
        commit_msg = f"Auto-update EPI from HDC API (ปีงบประมาณ {year_label} | ข้อมูล ณ วันที่ {latest})"
        subprocess.run([GIT_EXE, 'commit', '-m', commit_msg], cwd=GIT_DIR, check=True,
                       capture_output=True, text=True)
        subprocess.run([GIT_EXE, 'pull', '--rebase', 'origin', 'main'], cwd=GIT_DIR, check=True,
                       capture_output=True, text=True)
        subprocess.run([GIT_EXE, 'push', 'origin', 'main'], cwd=GIT_DIR, check=True,
                       capture_output=True, text=True)
        print("🎉 ดันข้อมูล EPI ขึ้น GitHub Pages เรียบร้อยแล้ว!")
        print("🔗 https://zole3501-kls.github.io/MMR2/EPIdashboard.html")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ git {e.cmd[1]} ล้มเหลว (exit {e.returncode})")
        detail = (e.stderr or e.stdout or '').strip()
        if detail:
            print(f"   {detail}")
        return False


def main():
    print("=" * 60)
    print("  ระบบดึงข้อมูล HDC อัตโนมัติและอัพเดท EPI แดชบอร์ด")
    print("  EPI ปกติ 22 เข็มที่เหลือ · รายงาน 1.1 + 2.1")
    print("=" * 60)

    fy = current_fiscal_year()
    target_years = [y for y in (fy - i for i in range(HISTORY_YEARS)) if y >= EPI_FIRST_YEAR]
    print(f"📅 ปีงบประมาณปัจจุบัน = {fy} | จะดึงปี: {', '.join(str(y) for y in target_years)}")

    with open(HTML_PATH, 'r', encoding='utf-8') as f:
        html_content = f.read()
    metadata_map, years = parse_existing(html_content)
    for code, meta in load_mmr2_metadata().items():
        metadata_map.setdefault(code, meta)

    obsolete = sorted(y for y in years if int(y) < EPI_FIRST_YEAR)
    for y in obsolete:
        del years[y]
    if obsolete:
        print(f"🗑️  ตัดปีงบประมาณ {', '.join(obsolete)} ออก (แสดงเฉพาะปี {EPI_FIRST_YEAR} เป็นต้นไป)")

    fetched_any = False
    api_failed = []
    verify_failed = []
    for year in target_years:
        datecom, got, reason = fetch_hdc_data(year)
        key = str(year)
        if reason == 'error':
            print(f"⏭️  ปี {year}: เรียก API ไม่สำเร็จ — คงข้อมูลเดิมไว้")
            api_failed.append(year)
            continue
        if not datecom:
            prev = years.get(key)
            if prev and prev['datecom']:
                print(f"⏭️  ปี {year}: HDC ตอบว่าง — คงข้อมูลเดิมที่มีไว้ ไม่เขียนทับด้วยศูนย์")
            else:
                years[key] = {'datecom': None, 'rows': '', 'note': reason}
            continue
        new_rows = generate_year_rows(got, metadata_map)
        problems = verify_year_rows(new_rows, got, year)
        if problems:
            print(f"❌ ปี {year}: cross-check ไม่ผ่าน — คงข้อมูลเดิมไว้ ไม่เผยแพร่ตัวเลขที่รู้ว่าผิด")
            for p in problems:
                print(f"   - {p}")
            verify_failed.append(year)
            continue
        years[key] = {'datecom': datecom, 'rows': new_rows, 'note': None}
        fetched_any = True
        print(f"🔎 ปี {year}: cross-check ผ่าน (จำนวนแถว/คอลัมน์/ยอดรวมตรงกับ API ทั้งสองรายงาน)")

    # ปีใหม่ (2570) ว่างเป็นเรื่องปกติของต้นทาง -> เขียนป้ายรอได้และ exit 0
    # ปฏิเสธการเขียนเมื่อไม่มีข้อมูลเลย *และ* มีอะไรล้มเหลวจริงเท่านั้น
    if not fetched_any and not any(y['datecom'] for y in years.values()) and (api_failed or verify_failed):
        print("❌ ไม่มีข้อมูลปีงบประมาณใดเลย และ API/cross-check ล้มเหลว — ยกเลิกการเขียนไฟล์")
        sys.exit(1)

    update_html(years)
    git_ok = run_git(years)

    print("=" * 60)
    for year in sorted(years, reverse=True):
        entry = years[year]
        if entry['datecom']:
            units = len([l for l in entry['rows'].split('\n') if l.strip()])
            print(f"  ปี {year}: {units} หน่วยบริการ | ข้อมูล ณ วันที่ {entry['datecom']}")
        else:
            print(f"  ปี {year}: ยังไม่มีข้อมูลใน HDC")
    if api_failed:
        print(f"  ❌ เรียก HDC API ไม่สำเร็จ: ปี {', '.join(str(y) for y in api_failed)}")
    if verify_failed:
        print(f"  ❌ cross-check ไม่ผ่าน: ปี {', '.join(str(y) for y in verify_failed)}")
    if not git_ok:
        print("  ❌ ส่งข้อมูลขึ้น GitHub ไม่สำเร็จ")
    if api_failed or verify_failed or not git_ok:
        print("  จบแบบมีข้อผิดพลาด — exit 1 (GitHub Actions จะขึ้นแดงและแจ้งเตือน)")
        print("=" * 60)
        sys.exit(1)
    print("  ดำเนินการสำเร็จเรียบร้อย!")
    print("=" * 60)


if __name__ == '__main__':
    main()