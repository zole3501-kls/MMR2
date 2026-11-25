# -*- coding: utf-8 -*-
"""
ระบบดึงข้อมูลจาก HDC API เพื่ออัพเดทรายงานความครอบคลุมวัคซีน PCV เข็ม 2 และ 4 เดือนอัตโนมัติ
================================================================
ใช้รายงาน 1.1 "ความครอบคลุมของการได้รับวัคซีนตามเกณฑ์ในเด็กอายุครบ 1 ปี รายไตรมาส"
ซึ่งเป็นคนละรายงานกับ MMR2 (รายงาน 2.1 เด็กอายุครบ 2 ปี)

ตัวชี้วัดที่แสดง:
  pcv1 = PCV 2 เดือน
  pcv2 = PCV 4 เดือน
ทั้งสองเข็มใช้เป้าหมายร่วมกัน (targetq1..q4) ตามสูตรของ HDC เอง
ส่วน pcv3 = "PCV 6 เดือน (นอกแผน)" ไม่เอามาแสดงเพราะไม่อยู่ในแผนงาน

วิธีใช้:
  1. รัน: uv run sync_hdc_pcv.py
  2. ทดสอบโดยไม่ push: PCV_NO_PUSH=1 uv run sync_hdc_pcv.py

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
from hdc_http import fetch_json_with_retry

# Set output encoding to UTF-8 to prevent encoding errors on Windows
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

BANGKOK = timezone(timedelta(hours=7))
EXCHANGE_ID = "c88658f4d09acb8c76e6c06ffb58ebaf"
QUARTERS = (1, 2, 3, 4)
# คอลัมน์ผลงานที่แสดง เรียงตามลำดับในแถวข้อมูล (เป้าหมายใช้ร่วมกันทั้งสองเข็ม)
INDICATORS = ('pcv1', 'pcv2')
# PCV เพิ่งบรรจุเข้าแผนงาน รายงาน 1.1 เริ่มมีคอลัมน์ pcv ปีงบประมาณ 2569
# แต่ผู้ใช้ต้องการดูปี 2570 ซึ่งเป็นปีแรก/full year จึงตั้งพื้นไว้ที่ 2570
PCV_FIRST_YEAR = 2570
HISTORY_YEARS = 1


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
    # Check script directory and parent directory first (for GitHub Actions / portable runs)
    if os.path.isdir(os.path.join(SCRIPT_DIR, '.git')):
        return SCRIPT_DIR
    parent_dir = os.path.dirname(SCRIPT_DIR)
    if os.path.isdir(os.path.join(parent_dir, '.git')):
        return parent_dir

    paths_to_check = [
        os.path.join(r"C:\GitRepos", repo_name),
    ]
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


# Find paths dynamically
GIT_EXE = find_git_exe()
GIT_DIR = find_repo_dir('MMR2')
if not GIT_DIR:
    print("❌ ไม่พบโฟลเดอร์ Git Repository ของ MMR2")
    sys.exit(1)

HTML_PATH = os.path.join(GIT_DIR, 'PCVdashboard.html')
MMR2_HTML_PATH = os.path.join(GIT_DIR, 'dashboard.html')
PUSH_TO_GITHUB = os.environ.get('PCV_NO_PUSH') != '1'


def current_fiscal_year():
    now = datetime.now(BANGKOK)
    return now.year + 543 + (1 if now.month >= 10 else 0)


def num(value):
    """แปลงค่าจาก API เป็น float; คืน None ถ้าเป็น null/ว่าง/ไม่ใช่ตัวเลข"""
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


def has_any_value(item):
    """หน่วยบริการนี้มีข้อมูลอย่างน้อยหนึ่งไตรมาสหรือไม่ (หน่วยที่ว่างทุกไตรมาสจะถูกข้าม)"""
    return any(num(item.get(f'{p}q{q}')) is not None
               for p in ('target',) + INDICATORS for q in QUARTERS)


def fetch_hdc_data(year):
    """คืน (datecom, inner_data, reason)
    - (None, None, 'error')        = เรียก API ไม่สำเร็จ (อย่าแตะข้อมูลเดิม)
    - ('', [], 'not_published')    = HDC ยังไม่เผยแพร่ข้อมูลของปีนี้
    - (datecom, [...], None)       = มีข้อมูล
    """
    print(f"🌐 กำลังดึงข้อมูล PCV จาก HDC API ปีงบประมาณ {year}...")
    url = (
        f"https://api-hdc.moph.go.th/v1/reports/province/data/{EXCHANGE_ID}"
        f"?table_display=provider&year={year}&month=ALL&zone=07&province_code=46"
        "&district_code=ALL&subdistrict_code=ALL&department_code=ALL&organization_type=ALL"
        "&ministry=ALL&hospital=ALL&service_plan=ALL&jurisdiction_code=ALL"
        "&freeze_month=ALL&mental_code=ALL&mental_group_code=ALL&custom=[]"
    )

    data_json, err = fetch_json_with_retry(url, label=f"PCV-{year}", timeout=30)
    if err or not data_json:
        print(f"❌ เรียก HDC API ปี {year} ไม่สำเร็จ: {err}")
        return None, None, 'error'

    if not data_json.get('ok'):
        print(f"❌ HDC API ปี {year} ตอบกลับ ok: false")
        return None, None, 'error'

    rows = data_json.get('rows') or []
    if not rows:
        print(f"⚠️  HDC API ปี {year} ไม่มี rows")
        return '', [], 'not_published'

    datecom_raw = rows[0].get('datecom')
    inner_data = rows[0].get('data') or []

    if not datecom_raw or not inner_data:
        print(f"⚠️  ปีงบประมาณ {year} ยังไม่มีข้อมูลในระบบ HDC (datecom={datecom_raw!r})")
        return '', [], 'not_published'

    if 'เวลา' not in datecom_raw and ':' not in datecom_raw:
        now_bkk = datetime.now(BANGKOK)
        datecom = f"{datecom_raw} เวลา {now_bkk.strftime('%H:%M')} น."
    else:
        datecom = datecom_raw

    has_pcv = any(num(item.get(f'{p}q{q}')) is not None
                  for item in inner_data for p in INDICATORS for q in QUARTERS)
    if not has_pcv:
        print(f"⚠️  ปีงบประมาณ {year} ยังไม่มีข้อมูลในระบบ HDC (ไม่มีผลงาน PCV)")
        return '', [], 'not_published'

    usable = sum(1 for item in inner_data if has_any_value(item))
    print(f"✅ ปี {year}: ดึงข้อมูลสำเร็จ ณ วันที่ {datecom} "
          f"จำนวน {usable} หน่วยบริการที่มีข้อมูล (API ส่งมา {len(inner_data)})")
    return datecom, inner_data, None


PCV_BLOCK_RE = re.compile(r'const PCV_DATA = \{[\s\S]*?\n\};')
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
    """คืน (metadata_map, years) โดย years = {ปี: {'datecom': str|None, 'rows': str}}"""
    metadata_map = {}
    years = {}

    block = PCV_BLOCK_RE.search(html_content)
    if not block:
        print("❌ ไม่พบบล็อกข้อมูล PCV_DATA ในไฟล์ HTML")
        sys.exit(1)

    for m in YEAR_ENTRY_RE.finditer(block.group(0)):
        year, datecom_raw, rows, note_raw = m.group(1), m.group(2), m.group(3), m.group(4)
        datecom = None if datecom_raw == 'null' else json.loads(datecom_raw)
        note = None if not note_raw or note_raw == 'null' else json.loads(note_raw)
        years[year] = {'datecom': datecom, 'rows': rows.strip(), 'note': note}
    # รวมเมตาดาต้าจากทุกปีที่มีข้อมูล โดยปีล่าสุดมาก่อน
    for year in sorted(years, reverse=True):
        for line in years[year]['rows'].split('\n'):
            if line.strip():
                parse_row_metadata(line.strip(), metadata_map)
    print(f"✅ อ่านข้อมูลเดิมสำเร็จ ({len(years)} ปี, {len(metadata_map)} หน่วยบริการ)")
    return metadata_map, years


def load_mmr2_metadata():
    """ยืมอำเภอ/สังกัด/idx จากแดชบอร์ด MMR2 เพราะ API ไม่ส่งข้อมูลพื้นที่มาด้วย
    และทั้งสองรายงานใช้หน่วยบริการชุดเดียวกันของจังหวัดกาฬสินธุ์"""
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


def generate_year_rows(inner_data, metadata_map):
    """สร้างแถวข้อมูล 16 คอลัมน์:
    idx,district,code:name,agency,t1,pcv1q1,pcv2q1,t2,pcv1q2,pcv2q2,t3,...,t4,pcv1q4,pcv2q4"""
    rows_data = []

    for item in inner_data:
        a_code = (item.get('a_code') or '').strip()
        if not a_code or not has_any_value(item):
            continue

        values = []
        for q in QUARTERS:
            values.append(num(item.get(f'targetq{q}')))
            for p in INDICATORS:
                values.append(num(item.get(f'{p}q{q}')))

        if a_code in metadata_map:
            idx, district, unit_full, agency = metadata_map[a_code]
        else:
            idx = 999
            district = ""
            a_name = (item.get('a_name') or '').strip()
            unit_name_clean = a_name.split(':')[1].strip() if ':' in a_name else a_name
            unit_full = f"{a_code}:{unit_name_clean}"
            agency = "สำนักงานสาธารณสุขจังหวัด"
            metadata_map[a_code] = (idx, district, unit_full, agency)

        cells = [str(idx), district, unit_full, agency] + [fmt_num(v) for v in values]
        rows_data.append((idx, ','.join(cells)))

    rows_data.sort(key=lambda x: x[0])
    return '\n'.join(line for _, line in rows_data)


def escape_template(text):
    return text.replace('\\', '\\\\').replace('`', '\\`').replace('${', '\\${')


def verify_year_rows(rows_text, inner_data, year):
    """ตรวจแถวที่เพิ่งสร้างกับข้อมูลดิบจาก API — จับบั๊กไตรมาสหาย/คอลัมน์เลื่อน/แถวหลุด
    คืนรายการปัญหาแบบร้ายแรง; ว่าง = ผ่าน"""
    problems = []
    source = [it for it in inner_data
              if (it.get('a_code') or '').strip() and has_any_value(it)]

    prefixes = ('target',) + INDICATORS
    expected = {p: 0.0 for p in prefixes}
    annual = {p: 0.0 for p in prefixes}
    has_annual = {p: False for p in prefixes}
    for item in source:
        for prefix in prefixes:
            for q in QUARTERS:
                v = num(item.get(f'{prefix}q{q}'))
                if v is not None:
                    expected[prefix] += v
            whole = num(item.get(prefix))
            if whole is not None:
                annual[prefix] += whole
                has_annual[prefix] = True

    actual = {p: 0.0 for p in prefixes}
    actual_rows = 0
    for line in rows_text.split('\n'):
        if not line.strip():
            continue
        cols = line.split(',')
        if len(cols) != 16:
            problems.append(f'แถวมี {len(cols)} คอลัมน์ (ต้อง 16): {line[:70]}')
            continue
        actual_rows += 1
        for q in range(4):
            for i, prefix in enumerate(prefixes):
                cell = cols[4 + q * 3 + i].strip()
                if not cell:
                    continue
                v = num(cell)
                if v is None:
                    problems.append(f'คอลัมน์ {prefix}q{q + 1} ไม่ใช่ตัวเลข: {cell!r}')
                else:
                    actual[prefix] += v

    if actual_rows != len(source):
        problems.append(f'จำนวนแถวไม่ตรง: API มี {len(source)} หน่วย แต่สร้างได้ {actual_rows} แถว')

    for prefix in prefixes:
        if abs(actual[prefix] - expected[prefix]) > 0.01:
            problems.append(
                f'ยอดรวม {prefix} ไม่ตรงผลรวมรายไตรมาสของ API: '
                f'API={fmt_num(expected[prefix])} แต่ในแถวที่สร้าง={fmt_num(actual[prefix])} '
                f'(ต่าง {fmt_num(actual[prefix] - expected[prefix])})')
        # ยอดทั้งปีของ API เองเป็นแค่คำเตือน: ปีที่ยังไม่ครบไตรมาสอาจให้ค่าทั้งปีมาเป็นแผน ไม่ใช่ผลงาน
        if has_annual[prefix] and abs(expected[prefix] - annual[prefix]) > 0.01:
            print(f'⚠️  ปี {year}: ผลรวมรายไตรมาสของ {prefix} ({fmt_num(expected[prefix])}) '
                  f'ไม่ตรงกับยอดทั้งปีที่ API ให้มา ({fmt_num(annual[prefix])}) — ตรวจสอบข้อมูลต้นทาง')

    return problems


def render_pcv_block(years):
    parts = []
    for year in sorted(years, reverse=True):
        entry = years[year]
        datecom_js = 'null' if not entry['datecom'] else json.dumps(entry['datecom'], ensure_ascii=False)
        rows_js = escape_template(entry['rows'])
        note = entry.get('note')
        note_js = '' if not note else ',\n    note: ' + json.dumps(note, ensure_ascii=False)
        parts.append(f'  "{year}": {{\n    datecom: {datecom_js},\n    rows: `{rows_js}`{note_js}\n  }}')
    return 'const PCV_DATA = {\n' + ',\n'.join(parts) + '\n};'


def update_html(years):
    print("📝 แก้ไขไฟล์ PCVdashboard.html...")
    with open(HTML_PATH, 'r', encoding='utf-8') as f:
        html_content = f.read()

    new_block = render_pcv_block(years)

    if not PCV_BLOCK_RE.search(html_content):
        print("❌ ไม่พบตำแหน่งสำหรับเขียนบล็อกข้อมูล")
        sys.exit(1)
    new_html = PCV_BLOCK_RE.sub(lambda m: new_block, html_content, count=1)

    with open(HTML_PATH, 'w', encoding='utf-8') as f:
        f.write(new_html)

    print("✅ เขียนไฟล์สำเร็จ!")


def run_git(years):
    """คืน True เมื่อ push สำเร็จหรือไม่มีอะไรต้อง commit, False เมื่อล้มเหลว (ผู้เรียกต้อง exit 1)"""
    if not PUSH_TO_GITHUB:
        print("⏭️  ข้ามการ push ขึ้น GitHub (ตั้งค่า PCV_NO_PUSH=1)")
        return True

    if not os.path.exists(GIT_EXE):
        print(f"❌ ไม่พบ git.exe ที่ {GIT_EXE}")
        return False

    with_data = [y for y in sorted(years, reverse=True) if years[y]['datecom']]
    latest = years[with_data[0]]['datecom'] if with_data else 'ไม่ทราบวันที่'
    year_label = '/'.join(with_data) if with_data else 'ไม่มีข้อมูล'

    print(f"🚀 กำลังส่งออกข้อมูล PCV ขึ้น GitHub ที่: {GIT_DIR}")
    try:
        subprocess.run([GIT_EXE, 'add', 'PCVdashboard.html'], cwd=GIT_DIR, check=True,
                       capture_output=True, text=True)

        # แยก "ไม่มีอะไรเปลี่ยนแปลง" ออกจาก "commit พัง" ให้ขาด
        staged = subprocess.run([GIT_EXE, 'diff', '--cached', '--quiet'], cwd=GIT_DIR)
        if staged.returncode == 0:
            print("ℹ️  ไม่มีการเปลี่ยนแปลงของข้อมูล จึงไม่มี commit ใหม่")
            return True
        if staged.returncode != 1:
            print(f"❌ git diff --cached ล้มเหลว (exit {staged.returncode})")
            return False

        commit_msg = f"Auto-update PCV from HDC API (ปีงบประมาณ {year_label} | ข้อมูล ณ วันที่ {latest})"
        subprocess.run([GIT_EXE, 'commit', '-m', commit_msg], cwd=GIT_DIR, check=True,
                       capture_output=True, text=True)
        subprocess.run([GIT_EXE, 'pull', '--rebase', 'origin', 'main'], cwd=GIT_DIR, check=True,
                       capture_output=True, text=True)
        subprocess.run([GIT_EXE, 'push', 'origin', 'main'], cwd=GIT_DIR, check=True,
                       capture_output=True, text=True)
        print("🎉 ดันข้อมูล PCV ขึ้น GitHub Pages เรียบร้อยแล้ว!")
        print("🔗 https://zole3501-kls.github.io/MMR2/PCVdashboard.html")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ git {e.cmd[1]} ล้มเหลว (exit {e.returncode})")
        detail = (e.stderr or e.stdout or '').strip()
        if detail:
            print(f"   {detail}")
        return False


def main():
    print("=" * 60)
    print("  ระบบดึงข้อมูล HDC อัตโนมัติและอัพเดท PCV แดชบอร์ด")
    print("  PCV 2 เดือน (pcv1) + PCV 4 เดือน (pcv2) · รายงาน 1.1")
    print("=" * 60)

    fy = current_fiscal_year()
    target_years = [y for y in (fy - i for i in range(HISTORY_YEARS))
                    if y >= PCV_FIRST_YEAR]
    print(f"📅 ปีงบประมาณปัจจุบัน = {fy} | จะดึงปี: {', '.join(str(y) for y in target_years)}")

    with open(HTML_PATH, 'r', encoding='utf-8') as f:
        html_content = f.read()
    metadata_map, years = parse_existing(html_content)

    for code, meta in load_mmr2_metadata().items():
        metadata_map.setdefault(code, meta)

    # ปีที่รายงานยังไม่มีวัคซีน PCV ไม่เอามาแสดง รวมถึงของเดิมที่ค้างอยู่ในไฟล์
    obsolete = sorted(y for y in years if int(y) < PCV_FIRST_YEAR)
    for y in obsolete:
        del years[y]
    if obsolete:
        print(f"🗑️  ตัดปีงบประมาณ {', '.join(obsolete)} ออก (แสดงเฉพาะปี {PCV_FIRST_YEAR} เป็นต้นไป)")

    fetched_any = False
    api_failed = []
    verify_failed = []
    for year in target_years:
        datecom, inner_data, reason = fetch_hdc_data(year)
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

        new_rows = generate_year_rows(inner_data, metadata_map)
        problems = verify_year_rows(new_rows, inner_data, year)
        if problems:
            print(f"❌ ปี {year}: cross-check ไม่ผ่าน — คงข้อมูลเดิมไว้ ไม่เผยแพร่ตัวเลขที่รู้ว่าผิด")
            for p in problems:
                print(f"   - {p}")
            verify_failed.append(year)
            continue

        years[key] = {'datecom': datecom, 'rows': new_rows, 'note': None}
        fetched_any = True
        print(f"🔎 ปี {year}: cross-check ผ่าน (จำนวนแถว/คอลัมน์/ยอดรวมตรงกับ API)")

    # ต่างจากสคริปต์ MMR2: แดชบอร์ด PCV มีปีเดียวคือปีปัจจุบัน ซึ่ง HDC ยังไม่เผยแพร่ในช่วงต้นปี
    # สถานะ "not_published" จึงเป็นเรื่องปกติ ไม่ใช่ error — เขียนป้ายรอได้ตามปกติและ exit 0
    # จะปฏิเสธการเขียนก็ต่อเมื่อไม่มีข้อมูลเลย *และ* มีอะไรล้มเหลวจริง (API ล่ม / cross-check ไม่ผ่าน)
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