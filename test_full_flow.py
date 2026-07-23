import urllib.request
import json

print('=== 测试完整功能 ===')

print('\n1. 测试 /debug 接口')
data = json.dumps({"test": "hello"}).encode('utf-8')
req = urllib.request.Request('http://localhost:8000/debug', data=data, headers={'Content-Type': 'application/json'}, method='POST')
resp = urllib.request.urlopen(req)
result = json.loads(resp.read().decode())
print(f'✅ /debug 响应: request_id={result["request_id"][:8]}...')
print(f'   trace 长度: {len(result["trace"])}')

print('\n2. 测试 /api/dashboard/trace 接口')
req = urllib.request.Request(f'http://localhost:8000/api/dashboard/trace/{result["request_id"]}')
resp = urllib.request.urlopen(req)
trace_result = json.loads(resp.read().decode())
print(f'✅ /api/dashboard/trace 响应: {trace_result["trace_kind"]}')

print('\n3. 验证 PostgreSQL 数据')
import os  # noqa: E402  # 需先打印进度再设环境变量
os.environ['STORAGE_BACKEND'] = 'postgresql'
os.environ['PG_HOST'] = 'localhost'
os.environ['PG_PORT'] = '5432'
os.environ['PG_DATABASE'] = 'ai_debug_mcp'
os.environ['PG_USER'] = 'postgres'
os.environ['PG_PASSWORD'] = 'REDACTED'

from app.mcp.core.storage.pg_store import _get_pool, close_pool  # noqa: E402  # 需先设 PG 环境变量
pool = _get_pool()
conn = pool.getconn()
cur = conn.cursor()
cur.execute("SELECT count(*) FROM traces")
count = cur.fetchone()[0]
print(f'✅ PostgreSQL traces 表行数: {count}')
cur.execute("SELECT request_id FROM traces ORDER BY id DESC LIMIT 1")
last_rid = cur.fetchone()[0]
print(f'✅ 最后一条 trace request_id: {last_rid[:8]}...')
pool.putconn(conn)
close_pool()

print('\n=== 所有测试通过 ===')
