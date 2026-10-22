import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

# Isolate test data from the user's normal local database. Pytest may import
# another test module (and therefore app.main) before this module, so setting
# DATABASE_PATH alone is insufficient; point the already-loaded global at the
# module-scoped temporary database before calling init_db().
tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
tmp.close()
previous_database_path = os.environ.get("DATABASE_PATH")
os.environ["DATABASE_PATH"] = tmp.name
os.environ.pop("LLM_API_KEY", None)

import app.main as main_module  # noqa: E402
from app.main import app, init_db  # noqa: E402

previous_db_path = main_module.DB_PATH
main_module.DB_PATH = Path(tmp.name)
init_db()
client = TestClient(app)


def test_health():
    response = client.get('/api/health')
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'


def test_profile_round_trip():
    response = client.put('/api/profile', json={
        'display_name': '测试用户', 'goals': '完成 MVP', 'preferences': '先测试再开发'
    })
    assert response.status_code == 200
    assert response.json()['display_name'] == '测试用户'
    assert client.get('/api/profile').json()['goals'] == '完成 MVP'


def test_memory_create_list_delete():
    created = client.post('/api/memories', json={
        'content': '喜欢分阶段交付完整代码', 'category': 'preference', 'source': 'test', 'confidence': 1, 'confirmed': True
    })
    assert created.status_code == 200
    memory_id = created.json()['id']
    assert any(m['id'] == memory_id for m in client.get('/api/memories').json())
    assert client.delete(f'/api/memories/{memory_id}').json()['deleted'] is True


def test_document_and_approval_workflow():
    doc = client.post('/api/documents/text', json={'filename': 'note.md', 'content': '本周目标是完成个人 AI MVP'})
    assert doc.status_code == 200
    approval = client.post('/api/approvals', json={'action_type': 'create_task', 'payload': {'title': '完成测试'}})
    assert approval.status_code == 200
    approval_id = approval.json()['id']
    reviewed = client.post(f'/api/approvals/{approval_id}/review', json={'decision': 'approved'})
    assert reviewed.status_code == 200
    assert reviewed.json()['status'] == 'approved'
    duplicate = client.post(f'/api/approvals/{approval_id}/review', json={'decision': 'rejected'})
    assert duplicate.status_code == 409


def test_chat_local_demo():
    client.post('/api/memories', json={
        'content': '用户喜欢提供完整可运行代码', 'category': 'preference', 'source': 'test', 'confidence': 1, 'confirmed': True
    })
    response = client.post('/api/chat', json={'message': '我喜欢什么开发方式？'})
    assert response.status_code == 200
    assert response.json()['conversation_id'] > 0
    assert '本地演示模式' in response.json()['answer']


def teardown_module():
    main_module.DB_PATH = previous_db_path
    if previous_database_path is None:
        os.environ.pop("DATABASE_PATH", None)
    else:
        os.environ["DATABASE_PATH"] = previous_database_path
    try:
        os.unlink(tmp.name)
    except OSError:
        pass
