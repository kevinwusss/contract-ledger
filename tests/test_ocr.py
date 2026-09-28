import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from contractdb.db import connect
from contractdb.web import create_app


def test_scanned_pdf_upload_runs_local_ocr_without_confirming_fields(tmp_path):
    image = Image.new('RGB', (1600, 800), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 45)
    draw.text((90, 90), '验收测试文件，不是真实合同', font=font, fill='black')
    draw.text((90, 200), '合同编号：OCR-TEST-001', font=font, fill='black')
    draw.text((90, 310), '项目名称：OCR核验项目', font=font, fill='black')
    pdf = io.BytesIO()
    image.save(pdf, format='PDF', resolution=150)
    app = create_app({'TESTING': True, 'DATA_DIR': tmp_path / 'data', 'SYNC_EXTRACTION': True, 'RESUME_EXTRACTION': False})
    client = app.test_client()
    client.get('/setup')
    with client.session_transaction() as session:
        token = session['csrf']
    client.post('/setup', data={'csrf_token': token, 'username': 'ocrtester', 'password': 'ocr-test-password'})
    client.post('/login', data={'csrf_token': token, 'username': 'ocrtester', 'password': 'ocr-test-password'})
    with client.session_transaction() as session:
        token = session['csrf']
    response = client.post('/upload', data={'csrf_token': token, 'files': (io.BytesIO(pdf.getvalue()), '扫描验收测试.pdf')}, content_type='multipart/form-data')
    assert response.status_code == 302
    with connect(app.config['DATA_DIR']) as connection:
        row = connection.execute('SELECT * FROM documents').fetchone()
        assert row['extraction_status'] == 'ready', row['extraction_error']
        assert 'OCR' in row['extracted_text'] and 'TEST' in row['extracted_text']
        assert row['review_status'] == 'pending'
        assert row['company_id'] is None and row['amount_minor'] is None
