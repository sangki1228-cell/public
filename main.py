import html
import json
import os
import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / '.env')  # 로컬 실행용; Actions에서는 Secrets가 환경변수로 전달됨


def get_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f'필수 환경변수 {name}이(가) 없습니다.')
    return value


API_KEY = get_env('API_KEY')
SENDER_EMAIL = get_env('SENDER_EMAIL')
SENDER_PASSWORD = get_env('SENDER_PASSWORD')
RECEIVER_EMAILS = [v.strip() for v in get_env('RECEIVER_EMAIL').split(',') if v.strip()]
if not RECEIVER_EMAILS:
    raise RuntimeError('수신자 이메일이 없습니다.')

API_ENDPOINT = 'https://apis.data.go.kr/1471000/OvseaMnfctrService/getOvseaMnfctrInq'
SEARCH_COMPANY = '한국엠에스디'
CURRENT_DATA_FILE = BASE_DIR / 'current_data.json'
OLD_DATA_FILE = BASE_DIR / 'old_data.json'
FIELDS = {'mnfctrNm': '제조소', 'cntryNm': '국가', 'adres': '제조소주소',
          'regNo': '등록번호', 'compnyNm': '회사명', 'latitude': '위도',
          'longitude': '경도', 'bizrno': '사업자등록번호'}


def extract_items_from_response(data):
    if not isinstance(data, dict):
        raise ValueError('API 응답이 JSON 객체가 아닙니다.')
    header = data.get('header') or data.get('response', {}).get('header', {})
    if isinstance(header, dict):
        result_code = header.get('resultCode')
        if result_code is not None and str(result_code).strip() not in ('00', '0', 'NORMAL_SERVICE'):
            raise ValueError(f"API 오류: {result_code} {header.get('resultMsg', '')}")
    body = data.get('body')
    if body is None and isinstance(data.get('response'), dict):
        body = data['response'].get('body')
    if not isinstance(body, dict):
        raise ValueError('API 응답에 body가 없습니다.')
    items = body.get('items')
    if isinstance(items, dict):
        items = items.get('item', items)
    if items is None:
        items = []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        raise ValueError('API items 형식이 올바르지 않습니다.')
    return items, body.get('totalCount')


def fetch_api_data():
    print(f'🌐 API 조회: {SEARCH_COMPANY}')
    all_items = []
    page = 1
    page_size = 100
    while True:
        params = {'serviceKey': API_KEY, 'pageNo': str(page),
                  'numOfRows': str(page_size), 'type': 'json', 'BSSH_NM': SEARCH_COMPANY}
        response = requests.get(API_ENDPOINT, params=params, timeout=30)
        response.raise_for_status()
        items, total = extract_items_from_response(response.json())
        all_items.extend(items)
        if total is not None:
            try:
                if len(all_items) >= int(total):
                    break
            except (TypeError, ValueError):
                raise ValueError('API totalCount가 유효하지 않습니다.')
        if len(items) < page_size:
            break
        if page >= 100:
            raise RuntimeError('API 페이지 수가 예상 범위를 초과했습니다.')
        page += 1

    # 빈 응답은 정상 삭제로 처리하지 않음 (API 오류/필터 변경 가능성)
    if not all_items:
        raise RuntimeError('API 결과가 0건입니다. 기존 데이터를 보존하고 종료합니다.')

    result = []
    for item in all_items:
        if not isinstance(item, dict):
            raise ValueError('API 항목이 객체가 아닙니다.')
        unique_id = item.get('MFTR_PRMSN_NO') or f"{item.get('MFTR_NM', '')}-{item.get('MFTR_ADDR', '')}"
        if not str(unique_id).strip() or unique_id == '-':
            raise ValueError('제조소 고유 식별자를 만들 수 없습니다.')
        result.append({
            'id': unique_id, 'compnyNm': item.get('BSSH_NM', ''),
            'mnfctrNm': item.get('MFTR_NM', ''), 'adres': item.get('MFTR_ADDR', ''),
            'cntryNm': item.get('NATN_CD_NM', ''), 'regNo': item.get('MFTR_PRMSN_NO', ''),
            'latitude': item.get('MFTR_LATD', ''), 'longitude': item.get('MFTR_LGTD', ''),
            'bizrno': item.get('BIZRNO', '')
        })
    if len({str(i['id']) for i in result}) != len(result):
        raise ValueError('API 결과에 중복된 ID가 있습니다. 비교를 중단합니다.')
    print(f'✅ {len(result)}건 수집')
    return sorted(result, key=lambda i: str(i['id']))


def save_json_data(data, path):
    # 파일 저장 중 실패해도 기존 파일이 손상되지 않도록 임시 파일 사용
    temp = path.with_suffix(path.suffix + '.tmp')
    try:
        with temp.open('w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
            f.write('\n')
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    print(f'💾 저장 완료: {path.name}')


def load_json_data(path):
    if not path.exists():
        return None  # 첫 실행
    with path.open('r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, list) or any(not isinstance(i, dict) or 'id' not in i for i in data):
        raise ValueError(f'{path.name} 데이터 구조가 올바르지 않습니다.')
    return data


def compare_data(current, old):
    cur = {str(i['id']): i for i in current}
    prev = {str(i['id']): i for i in old}
    added = [cur[k] for k in sorted(cur.keys() - prev.keys())]
    deleted = [prev[k] for k in sorted(prev.keys() - cur.keys())]
    changed = []
    for k in sorted(cur.keys() & prev.keys()):
        if cur[k] != prev[k]:
            diffs = {field: {'old': prev[k].get(field, 'N/A'), 'new': cur[k].get(field, 'N/A')}
                     for field in sorted(cur[k].keys() | prev[k].keys())
                     if cur[k].get(field) != prev[k].get(field)}
            changed.append({'id': k, 'item': cur[k], 'changes': diffs})
    return added, deleted, changed


def format_results_for_email_html(added, deleted, changed):
    esc = lambda v: html.escape(str(v if v is not None else ''), quote=True)
    now = datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y-%m-%d %H:%M:%S KST')
    parts = [f'''<html><head><meta charset="utf-8"><style>
    body {{font-family:Arial,sans-serif;color:#333}}
    table {{border-collapse:collapse;margin:12px 0 24px}}
    th,td {{border:1px solid #ddd;padding:10px;text-align:left;vertical-align:top}}
    th {{background:#f2f2f2}} tr:nth-child(even) {{background:#fafafa}}
    </style></head><body><h2>📊 해외제조소 변경사항 분석 결과 - {esc(SEARCH_COMPANY)}</h2>
    <p>🕐 분석 시간: {esc(now)}</p>''']
    for label, records in [('새로 추가된 제조소', added), ('삭제된 제조소', deleted)]:
        if not records:
            continue
        parts.append(f'<h3>{esc(label)}: {len(records)}건</h3><table><tr><th>#</th><th>등록번호</th><th>국가</th><th>제조소명</th><th>주소</th></tr>')
        for idx, item in enumerate(records, 1):
            parts.append('<tr>' + ''.join(f'<td>{esc(v)}</td>' for v in
                         [idx, item.get('regNo') or 'N/A', item.get('cntryNm', ''),
                          item.get('mnfctrNm', ''), item.get('adres', '')]) + '</tr>')
        parts.append('</table>')
    if changed:
        parts.append(f'<h3>정보가 변경된 제조소: {len(changed)}건</h3><table><tr><th>#</th><th>등록번호</th><th>국가</th><th>제조소명</th><th>주소</th><th>변경사항</th></tr>')
        for idx, info in enumerate(changed, 1):
            item = info['item']
            details = '<ul>' + ''.join(
                f"<li><b>{esc(FIELDS.get(field, field))}</b>: '{esc(diff['old'])}' → '{esc(diff['new'])}'</li>"
                for field, diff in info['changes'].items() if field != 'id') + '</ul>'
            parts.append('<tr>' + ''.join(f'<td>{esc(v)}</td>' for v in
                         [idx, item.get('regNo') or 'N/A', item.get('cntryNm', ''),
                          item.get('mnfctrNm', ''), item.get('adres', '')]) + f'<td>{details}</td></tr>')
        parts.append('</table>')
    parts.append(f'<p><b>📈 요약: 추가 {len(added)}건 | 삭제 {len(deleted)}건 | 변경 {len(changed)}건</b></p></body></html>')
    return ''.join(parts)


def send_email(subject, body):
    print(f"📧 이메일 발송 시도 (수신자 {len(RECEIVER_EMAILS)}명)")
    msg = MIMEMultipart()
    msg['From'] = SENDER_EMAIL
    msg['To'] = ', '.join(RECEIVER_EMAILS)
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'html', 'utf-8'))
    with smtplib.SMTP('smtp.gmail.com', 587, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.send_message(msg)
    print('✅ 이메일 발송 성공')


def main():
    print(f'🚀 해외제조소 모니터링 시작: {SEARCH_COMPANY}')
    old_data = load_json_data(OLD_DATA_FILE)
    current_data = fetch_api_data()

    if old_data is None:
        # 기존 목록이 없으면 기준 데이터만 만들고 이메일은 보내지 않음
        save_json_data(current_data, CURRENT_DATA_FILE)
        save_json_data(current_data, OLD_DATA_FILE)
        print('🆕 최초 실행: 기준 목록 저장, 이메일 미발송')
        return

    added, deleted, changed = compare_data(current_data, old_data)
    print(f'📈 추가 {len(added)}건 / 삭제 {len(deleted)}건 / 변경 {len(changed)}건')
    if added or deleted or changed:
        body = format_results_for_email_html(added, deleted, changed)
        send_email(f'[해외제조소 변경 감지] {SEARCH_COMPANY} 현황 업데이트', body)
        # 이메일 발송 성공 후에만 이전 목록 갱신
        save_json_data(current_data, OLD_DATA_FILE)
    else:
        print('👍 변경사항 없음: 이메일 미발송')
    save_json_data(current_data, CURRENT_DATA_FILE)
    print('✅ 모니터링 완료')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'❌ 실행 실패: {exc}', file=sys.stderr)
        sys.exit(1)
