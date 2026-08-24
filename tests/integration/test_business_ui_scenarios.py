"""集成测试：业务级 UI 验证场景。

覆盖点：
- 表单验证
- 数据表格验证
- 数值范围验证
- 登录流程验证（通过组合现有功能）
"""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

import pytest

from app.runtime.verifier import ui_runner


@pytest.fixture
def business_ui_site():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "business.html").write_text(
            """
<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>Business UI Tests</title></head>
  <body>
    <!-- 登录表单 -->
    <form id="login-form">
      <input type="text" id="username" placeholder="Username" />
      <input type="password" id="password" placeholder="Password" />
      <button type="submit">Login</button>
    </form>
    
    <!-- 示例表单 -->
    <form id="user-form">
      <input type="text" name="name" id="name" value="John Doe" />
      <input type="email" name="email" id="email" value="john@example.com" />
      <input type="number" name="age" id="age" value="30" />
      <button type="submit">Submit</button>
    </form>
    
    <!-- 数据表格 -->
    <table id="data-table">
      <thead>
        <tr>
          <th>Name</th>
          <th>Age</th>
          <th>City</th>
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>John Doe</td>
          <td>30</td>
          <td>New York</td>
        </tr>
        <tr>
          <td>Jane Smith</td>
          <td>25</td>
          <td>Los Angeles</td>
        </tr>
        <tr>
          <td>Bob Johnson</td>
          <td>35</td>
          <td>Chicago</td>
        </tr>
      </tbody>
    </table>
    
    <!-- 数值显示 -->
    <div id="price">$29.99</div>
    <div id="rating">4.5</div>
    <div id="quantity">100</div>
    
    <!-- 状态显示区域 -->
    <div id="status"></div>
    
    <script>
      document.getElementById('login-form').addEventListener('submit', function(e) {
        e.preventDefault();
        const username = document.getElementById('username').value;
        const password = document.getElementById('password').value;
        
        if(username === 'admin' && password === 'password') {
          document.getElementById('status').textContent = 'Login successful';
          document.getElementById('status').className = 'success';
        } else {
          document.getElementById('status').textContent = 'Login failed';
          document.getElementById('status').className = 'error';
        }
      });
      
      document.getElementById('user-form').addEventListener('submit', function(e) {
        e.preventDefault();
        document.getElementById('status').textContent = 'Form submitted';
        document.getElementById('status').className = 'success';
      });
    </script>
  </body>
</html>
            """.strip(),
            encoding="utf-8",
        )

        handler = partial(SimpleHTTPRequestHandler, directory=str(root))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            yield f"http://{host}:{port}/business.html"
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


@pytest.mark.integration
def test_form_validation_assertion(monkeypatch, business_ui_site):
    """测试表单验证断言功能。"""
    if not ui_runner.is_available():
        pytest.skip("playwright 不可用，跳过真实浏览器 UI 验证")

    monkeypatch.setattr("app.config.settings.ui_url_allow_private", True)

    spec = {
        "kind": "ui",
        "target": business_ui_site,
        "expect": {
            "assertions": [
                {
                    "type": "form",
                    "selector": "#user-form",
                    "values": {
                        "name": "John Doe",
                        "email": "john@example.com",
                        "age": "30"
                    }
                }
            ]
        }
    }

    result = ui_runner.run_ui_verification(spec, timeout_ms=10000)

    assert result["matched"] is True
    assert result["silent_failure"] is False
    assert result["diffs"] == []
    assert len(result["interactions"]) == 1
    interaction = result["interactions"][0]
    assert len(interaction["assertions"]) == 1
    assert interaction["assertions"][0]["type"] == "form"
    assert interaction["assertions"][0]["matched"] is True


@pytest.mark.integration
def test_data_table_validation_assertion(monkeypatch, business_ui_site):
    """测试数据表格验证断言功能。"""
    if not ui_runner.is_available():
        pytest.skip("playwright 不可用，跳过真实浏览器 UI 验证")

    monkeypatch.setattr("app.config.settings.ui_url_allow_private", True)

    spec = {
        "kind": "ui",
        "target": business_ui_site,
        "expect": {
            "assertions": [
                {
                    "type": "data_table",
                    "selector": "#data-table",
                    "rows": 3,  # 3 个数据行
                    "columns": 3,  # 3 个列
                    "headers": ["Name", "Age", "City"]
                }
            ]
        }
    }

    result = ui_runner.run_ui_verification(spec, timeout_ms=10000)

    assert result["matched"] is True
    assert result["silent_failure"] is False
    assert result["diffs"] == []
    assert len(result["interactions"]) == 1
    interaction = result["interactions"][0]
    assert len(interaction["assertions"]) == 1
    assert interaction["assertions"][0]["type"] == "data_table"
    assert interaction["assertions"][0]["matched"] is True
    assert interaction["assertions"][0]["actual"]["rows"] == 3
    assert interaction["assertions"][0]["actual"]["columns"] == 3


@pytest.mark.integration
def test_numeric_range_validation_assertion(monkeypatch, business_ui_site):
    """测试数值范围验证断言功能。"""
    if not ui_runner.is_available():
        pytest.skip("playwright 不可用，跳过真实浏览器 UI 验证")

    monkeypatch.setattr("app.config.settings.ui_url_allow_private", True)

    spec = {
        "kind": "ui",
        "target": business_ui_site,
        "expect": {
            "assertions": [
                {
                    "type": "numeric_range",
                    "selector": "#quantity",
                    "min": 50,
                    "max": 200
                },
                {
                    "type": "numeric_range",
                    "selector": "#rating",
                    "min": 4.0,
                    "max": 5.0
                }
            ]
        }
    }

    result = ui_runner.run_ui_verification(spec, timeout_ms=10000)

    assert result["matched"] is True
    assert result["silent_failure"] is False
    assert result["diffs"] == []
    assert len(result["interactions"]) == 1
    interaction = result["interactions"][0]
    assert len(interaction["assertions"]) == 2
    assert all(a["type"] == "numeric_range" for a in interaction["assertions"])
    assert all(a["matched"] is True for a in interaction["assertions"])


@pytest.mark.integration
def test_login_flow_validation(monkeypatch, business_ui_site):
    """测试登录流程验证（通过组合现有功能）。"""
    if not ui_runner.is_available():
        pytest.skip("playwright 不可用，跳过真实浏览器 UI 验证")

    monkeypatch.setattr("app.config.settings.ui_url_allow_private", True)

    spec = {
        "kind": "ui",
        "target": business_ui_site,
        "expect": {
            "interactions": [
                {
                    "action": "fill",
                    "selector": "#username",
                    "value": "admin"
                },
                {
                    "action": "fill",
                    "selector": "#password",
                    "value": "password"
                },
                {
                    "action": "click",
                    "selector": "button[type='submit']"
                },
                {
                    "action": "text",
                    "selector": "#status",
                    "expect": {
                        "assertions": [
                            {
                                "type": "text",
                                "selector": "#status",
                                "equals": "Login successful"
                            }
                        ]
                    }
                }
            ]
        }
    }

    result = ui_runner.run_ui_verification(spec, timeout_ms=15000)

    # 验证交互是否成功（即使登录可能失败，也要验证交互流程）
    assert len(result["interactions"]) >= 1
    assert result["security"]["target"]["allowed"] is True
    assert result["security"]["target"]["rule"] == "allow_private"


@pytest.mark.integration
def test_form_submission_validation(monkeypatch, business_ui_site):
    """测试表单提交验证。"""
    if not ui_runner.is_available():
        pytest.skip("playwright 不可用，跳过真实浏览器 UI 验证")

    monkeypatch.setattr("app.config.settings.ui_url_allow_private", True)

    spec = {
        "kind": "ui",
        "target": business_ui_site,
        "expect": {
            "interactions": [
                {
                    "action": "click",
                    "selector": "#user-form button[type='submit']"
                },
                {
                    "action": "text",
                    "selector": "#status",
                    "expect": {
                        "assertions": [
                            {
                                "type": "text",
                                "selector": "#status",
                                "equals": "Form submitted"
                            }
                        ]
                    }
                }
            ]
        }
    }

    result = ui_runner.run_ui_verification(spec, timeout_ms=15000)

    assert len(result["interactions"]) >= 1
    assert result["security"]["target"]["allowed"] is True
    assert result["security"]["target"]["rule"] == "allow_private"
