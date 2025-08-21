import importlib
import importlib.metadata
import os
import shlex
import sys
import textwrap
import types

from flask import Flask, Response, send_from_directory
from packaging.version import Version

from mlflow.environment_variables import MLFLOW_FLASK_SERVER_SECRET_KEY
from mlflow.exceptions import MlflowException
from mlflow.server import handlers
from mlflow.server.handlers import (
    STATIC_PREFIX_ENV_VAR,
    _add_static_prefix,
    create_promptlab_run_handler,
    gateway_proxy_handler,
    get_artifact_handler,
    get_logged_model_artifact_handler,
    get_metric_history_bulk_handler,
    get_metric_history_bulk_interval_handler,
    get_model_version_artifact_handler,
    get_trace_artifact_handler,
    search_datasets_handler,
    upload_artifact_handler,
)
from mlflow.utils.os import is_windows
from mlflow.utils.plugins import get_entry_points
from mlflow.utils.process import _exec_cmd
from mlflow.version import VERSION

# NB: These are internal environment variables used for communication between
# the cli and the forked gunicorn processes.
BACKEND_STORE_URI_ENV_VAR = "_MLFLOW_SERVER_FILE_STORE"
REGISTRY_STORE_URI_ENV_VAR = "_MLFLOW_SERVER_REGISTRY_STORE"
ARTIFACT_ROOT_ENV_VAR = "_MLFLOW_SERVER_ARTIFACT_ROOT"
ARTIFACTS_DESTINATION_ENV_VAR = "_MLFLOW_SERVER_ARTIFACT_DESTINATION"
PROMETHEUS_EXPORTER_ENV_VAR = "prometheus_multiproc_dir"
SERVE_ARTIFACTS_ENV_VAR = "_MLFLOW_SERVER_SERVE_ARTIFACTS"
ARTIFACTS_ONLY_ENV_VAR = "_MLFLOW_SERVER_ARTIFACTS_ONLY"

REL_STATIC_DIR = "js/build"

app = Flask(__name__, static_folder=REL_STATIC_DIR)
IS_FLASK_V1 = Version(importlib.metadata.version("flask")) < Version("2.0")


for http_path, handler, methods in handlers.get_endpoints():
    app.add_url_rule(http_path, handler.__name__, handler, methods=methods)

if os.getenv(PROMETHEUS_EXPORTER_ENV_VAR):
    from mlflow.server.prometheus_exporter import activate_prometheus_exporter

    prometheus_metrics_path = os.getenv(PROMETHEUS_EXPORTER_ENV_VAR)
    if not os.path.exists(prometheus_metrics_path):
        os.makedirs(prometheus_metrics_path)
    activate_prometheus_exporter(app)


# Provide a health check endpoint to ensure the application is responsive
@app.route("/health")
def health():
    return "OK", 200


# Provide an endpoint to query the version of mlflow running on the server
@app.route("/version")
def version():
    return VERSION, 200


# Serve the "get-artifact" route.
@app.route(_add_static_prefix("/get-artifact"))
def serve_artifacts():
    return get_artifact_handler()


# Serve the "model-versions/get-artifact" route.
@app.route(_add_static_prefix("/model-versions/get-artifact"))
def serve_model_version_artifact():
    return get_model_version_artifact_handler()


# Serve the "metrics/get-history-bulk" route.
@app.route(_add_static_prefix("/ajax-api/2.0/mlflow/metrics/get-history-bulk"))
def serve_get_metric_history_bulk():
    return get_metric_history_bulk_handler()


# Serve the "metrics/get-history-bulk-interval" route.
@app.route(_add_static_prefix("/ajax-api/2.0/mlflow/metrics/get-history-bulk-interval"))
def serve_get_metric_history_bulk_interval():
    return get_metric_history_bulk_interval_handler()


# Serve the "experiments/search-datasets" route.
@app.route(_add_static_prefix("/ajax-api/2.0/mlflow/experiments/search-datasets"), methods=["POST"])
def serve_search_datasets():
    return search_datasets_handler()


# Serve the "runs/create-promptlab-run" route.
@app.route(_add_static_prefix("/ajax-api/2.0/mlflow/runs/create-promptlab-run"), methods=["POST"])
def serve_create_promptlab_run():
    return create_promptlab_run_handler()


@app.route(_add_static_prefix("/ajax-api/2.0/mlflow/gateway-proxy"), methods=["POST", "GET"])
def serve_gateway_proxy():
    return gateway_proxy_handler()


@app.route(_add_static_prefix("/ajax-api/2.0/mlflow/upload-artifact"), methods=["POST"])
def serve_upload_artifact():
    return upload_artifact_handler()


# Serve the "/get-trace-artifact" route to allow frontend to fetch trace artifacts
# and render them in the Trace UI. The request body should contain the request_id
# of the trace.
@app.route(_add_static_prefix("/ajax-api/2.0/mlflow/get-trace-artifact"), methods=["GET"])
def serve_get_trace_artifact():
    return get_trace_artifact_handler()


@app.route(
    _add_static_prefix("/ajax-api/2.0/mlflow/logged-models/<model_id>/artifacts/files"),
    methods=["GET"],
)
def serve_get_logged_model_artifact(model_id: str):
    return get_logged_model_artifact_handler(model_id)


# We expect the react app to be built assuming it is hosted at /static-files, so that requests for
# CSS/JS resources will be made to e.g. /static-files/main.css and we can handle them here.
# The files are hashed based on source code, so ok to send Cache-Control headers via max_age.
@app.route(_add_static_prefix("/static-files/<path:path>"))
def serve_static_file(path):
    if IS_FLASK_V1:
        return send_from_directory(app.static_folder, path, cache_timeout=2419200)
    else:
        return send_from_directory(app.static_folder, path, max_age=2419200)


# Serve the index.html for the React App for all other routes.
@app.route(_add_static_prefix("/"))
def serve():
    if os.path.exists(os.path.join(app.static_folder, "index.html")):
        # Check if auth system is enabled and inject navigation components
        auth_components = _get_auth_navigation_components()
        if auth_components:
            return _serve_index_with_auth_components(auth_components)
        return send_from_directory(app.static_folder, "index.html")

    text = textwrap.dedent(
        """
    Unable to display MLflow UI - landing page (index.html) not found.

    You are very likely running the MLflow server using a source installation of the Python MLflow
    package.

    If you are a developer making MLflow source code changes and intentionally running a source
    installation of MLflow, you can view the UI by running the Javascript dev server:
    https://github.com/mlflow/mlflow/blob/master/CONTRIBUTING.md#running-the-javascript-dev-server

    Otherwise, uninstall MLflow via 'pip uninstall mlflow', reinstall an official MLflow release
    from PyPI via 'pip install mlflow', and rerun the MLflow server.
    """
    )
    return Response(text, mimetype="text/plain")


def _get_auth_navigation_components():
    """Check if auth system is enabled and return navigation components"""
    try:
        # Try to import auth module to check if it's active
        from flask import session
        from mlflow.server.auth import _get_current_user_session
        
        # Check if user is logged in
        user_info = _get_current_user_session()
        if user_info:
            return {
                'username': user_info.get('username'),
                'is_admin': user_info.get('is_admin', False),
                'logout_url': '/logout',
                'admin_url': '/admin'
            }
    except (ImportError, AttributeError, Exception):
        # Auth system not active or not configured
        pass
    return None


def _serve_index_with_auth_components(auth_components):
    """Serve index.html with injected auth navigation components and Docker build feature"""
    import os
    from flask import Response
    
    # Read the original index.html
    index_path = os.path.join(app.static_folder, "index.html")
    with open(index_path, 'r', encoding='utf-8') as f:
        html_content = f.read()
    
    # Inject auth navigation components
    auth_nav_html = _generate_auth_navigation_html(auth_components)
    
    # Inject Docker build component (only for admin users)
    docker_build_html = ""
    if auth_components.get('is_admin', False):
        docker_build_html = _generate_docker_build_html()
    
    # Insert components before closing body tag
    html_content = html_content.replace('</body>', f'{auth_nav_html}{docker_build_html}</body>')
    
    return Response(html_content, mimetype='text/html')


def _generate_auth_navigation_html(auth_components):
    """Generate HTML for auth navigation components"""
    username = auth_components['username']
    is_admin = auth_components['is_admin']
    logout_url = auth_components['logout_url']
    admin_url = auth_components['admin_url']
    
    admin_button = ""
    if is_admin:
        admin_button = f'''
        <button id="mlflow-admin-btn" onclick="window.location.href='{admin_url}'" 
                title="管理面板">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" style="margin-right: 6px;">
                <path d="M12,15.5A3.5,3.5 0 0,1 8.5,12A3.5,3.5 0 0,1 12,8.5A3.5,3.5 0 0,1 15.5,12A3.5,3.5 0 0,1 12,15.5M19.43,12.97C19.47,12.65 19.5,12.33 19.5,12C19.5,11.67 19.47,11.34 19.43,11.03L21.54,9.37C21.73,9.22 21.78,8.95 21.66,8.73L19.66,5.27C19.54,5.05 19.27,4.96 19.05,5.05L16.56,6.05C16.04,5.66 15.5,5.32 14.87,5.07L14.5,2.42C14.46,2.18 14.25,2 14,2H10C9.75,2 9.54,2.18 9.5,2.42L9.13,5.07C8.5,5.32 7.96,5.66 7.44,6.05L4.95,5.05C4.73,4.96 4.46,5.05 4.34,5.27L2.34,8.73C2.22,8.95 2.27,9.22 2.46,9.37L4.57,11.03C4.53,11.34 4.5,11.67 4.5,12C4.5,12.33 4.53,12.65 4.57,12.97L2.46,14.63C2.27,14.78 2.22,15.05 2.34,15.27L4.34,18.73C4.46,18.95 4.73,19.03 4.95,18.95L7.44,17.94C7.96,18.34 8.5,18.68 9.13,18.93L9.5,21.58C9.54,21.82 9.75,22 10,22H14C14.25,22 14.46,21.82 14.5,21.58L14.87,18.93C15.5,18.68 16.04,18.34 16.56,17.94L19.05,18.95C19.27,19.03 19.54,18.95 19.66,18.73L21.66,15.27C21.78,15.05 21.73,14.78 21.54,14.63L19.43,12.97Z"/>
            </svg>
            管理面板
        </button>
        '''
    
    return f'''
    <div id="mlflow-auth-nav" style="position: fixed; bottom: 20px; left: 5px; z-index: 10000; 
         width: 200px; background: #1F272D; padding: 12px; border-radius: 8px; 
         box-shadow: 0 2px 8px rgba(0,0,0,0.1); border: 0px solid #e0e0e0;
         font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; 
         font-size: 13px;">
        <div style="color: #BAE1E5; margin-bottom: 8px; font-weight: 500; padding: 4px 0;">
            欢迎，{username}
        </div>
        <div style="display: flex; flex-direction: column; gap: 6px;">
            {admin_button}
            <button id="mlflow-logout-btn" onclick="window.location.href='{logout_url}'" 
                    title="登出">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" style="margin-right: 6px;">
                    <path d="M16,17V14H9V10H16V7L21,12L16,17M14,2A2,2 0 0,1 16,4V6H14V4H5V20H14V18H16V20A2,2 0 0,1 14,22H5A2,2 0 0,1 3,20V4A2,2 0 0,1 5,2H14Z"/>
                </svg>
                登出
            </button>
        </div>
    </div>
    <style>
        #mlflow-auth-nav button {{
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 12px;
            padding: 6px 12px;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            gap: 4px;
        }}
        #mlflow-admin-btn {{
            background: #30414F;
            color: #BAE1E5;
            border: 1px solid #ddd;
            width: 100%;
            justify-content: flex-start;
        }}
        #mlflow-admin-btn:hover {{
            background: #f5f5f5;
            border-color: #ccc;
        }}
        #mlflow-logout-btn {{
            background: #30414F;
            color: #BAE1E5;
            border: 1px solid #ddd;
            width: 100%;
            justify-content: flex-start;
        }}
        #mlflow-logout-btn:hover {{
            background: #fef5f5;
            border-color: #d32f2f;
        }}
        @media (max-width: 768px) {{
            #mlflow-auth-nav {{
                position: fixed;
                top: 5px;
                right: 5px;
                padding: 6px 8px;
                font-size: 12px;
            }}
            #mlflow-auth-nav span {{
                display: none;
            }}
        }}
    </style>
    '''


def _generate_docker_build_html():
    """生成通用Docker构建组件HTML"""
    return r'''
    <!-- 通用Docker构建组件 -->
    <div id="mlflow-docker-build-container" style="display: none;">
        <button id="mlflow-docker-build-btn" style="
            position: fixed; 
            top: 95px; 
            right: 185px; 
            z-index: 9999;
            background: #4299E0;
            color: black;
            border: none;
            padding: 6px 12px;
            border-radius: 6px;
            cursor: pointer;
            font-weight: 350;
            font-size: 13px;
            box-shadow: 0 2px 8px rgba(66,153,224,0.3);
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            transition: all 0.2s ease;
        " onmouseover="this.style.background='#4299E0'" onmouseout="this.style.background='#4299E0'">
            🐳构建Docker镜像
        </button>
    </div>
    
    <!-- 构建配置对话框 -->
    <div id="mlflow-docker-build-dialog" style="
        display: none;
        position: fixed;
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;
        background: rgba(0, 0, 0, 0.5);
        z-index: 10000;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    ">
        <div style="
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            background: white;
            border-radius: 8px;
            padding: 24px;
            width: 400px;
            max-width: 90vw;
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.15);
        ">
            <div style="
                font-size: 18px;
                font-weight: 600;
                margin-bottom: 16px;
                color: #333;
            ">🐳 Docker镜像构建配置</div>
            
            <div style="margin-bottom: 16px;">
                <label style="
                    display: block;
                    margin-bottom: 6px;
                    font-weight: 500;
                    color: #555;
                ">镜像名称:</label>
                <input type="text" id="docker-image-name" style="
                    width: 100%;
                    padding: 8px 12px;
                    border: 1px solid #d9d9d9;
                    border-radius: 4px;
                    font-size: 14px;
                    box-sizing: border-box;
                " placeholder="my-model-v1">
            </div>
            
            <div style="margin-bottom: 20px;">
                <label style="
                    display: block;
                    margin-bottom: 6px;
                    font-weight: 500;
                    color: #555;
                ">基础镜像 (可选):</label>
                <input type="text" id="docker-base-image" style="
                    width: 100%;
                    padding: 8px 12px;
                    border: 1px solid #d9d9d9;
                    border-radius: 4px;
                    font-size: 14px;
                    box-sizing: border-box;
                " placeholder="留空让MLflow自动选择最佳镜像，或输入自定义镜像">
                <div style="
                    font-size: 12px;
                    color: #666;
                    margin-top: 4px;
                ">💡 留空: MLflow自动选择 | 示例: python:3.12-slim, ubuntu:20.04, nvidia/cuda:11.8-runtime-ubuntu20.04</div>
            </div>
            
            <div style="
                display: flex;
                justify-content: flex-end;
                gap: 12px;
            ">
                <button onclick="cancelBuild()" style="
                    background: #f5f5f5;
                    color: #666;
                    border: 1px solid #d9d9d9;
                    padding: 8px 16px;
                    border-radius: 4px;
                    cursor: pointer;
                    font-size: 14px;
                    ">取消</button>
                <button onclick="startBuild()" style="
                    background: #1890ff;
                    color: white;
                    border: none;
                    padding: 8px 16px;
                    border-radius: 4px;
                    cursor: pointer;
                    font-size: 14px;
                ">开始构建</button>
            </div>
        </div>
    </div>
    
    <script>
    (function() {
        'use strict';
        
        // 检测当前页面是否为模型版本页面
        function isModelVersionPage() {
            const hash = window.location.hash;
            return hash.match(/^#\/models\/[^\/]+\/versions\/[^\/]+$/);
        }
        
        // 显示Docker构建按钮
        function showDockerBuildButton() {
            const container = document.getElementById('mlflow-docker-build-container');
            if (container && isModelVersionPage()) {
                container.style.display = 'block';
            } else if (container) {
                container.style.display = 'none';
            }
        }
        
        // 解析当前模型信息
        function getCurrentModelInfo() {
            const hash = window.location.hash;
            const match = hash.match(/^#\/models\/([^\/]+)\/versions\/([^\/]+)$/);
            if (match) {
                return {
                    modelName: decodeURIComponent(match[1]),
                    modelVersion: match[2]
                };
            }
            return null;
        }
        
        // 显示构建对话框
        function showBuildDialog() {
            const modelInfo = getCurrentModelInfo();
            if (!modelInfo) {
                alert('无法获取模型信息');
                return;
            }
            
            // 设置默认镜像名称
            const defaultImageName = modelInfo.modelName.toLowerCase().replace(/[^a-z0-9-]/g, '-') + '-v' + modelInfo.modelVersion;
            document.getElementById('docker-image-name').value = defaultImageName;
            document.getElementById('docker-base-image').value = '';
            
            // 显示对话框
            document.getElementById('mlflow-docker-build-dialog').style.display = 'block';
        }
        
        // 全局函数：取消构建
        window.cancelBuild = function() {
            document.getElementById('mlflow-docker-build-dialog').style.display = 'none';
        };
        
        // 全局函数：开始构建
        window.startBuild = function() {
            const modelInfo = getCurrentModelInfo();
            if (!modelInfo) {
                alert('无法获取模型信息');
                return;
            }
            
            const imageName = document.getElementById('docker-image-name').value.trim();
            const baseImage = document.getElementById('docker-base-image').value.trim();
            
            if (!imageName) {
                alert('请输入镜像名称');
                return;
            }
            
            // 隐藏对话框
            cancelBuild();
            
            // 构建请求数据
            const requestData = {
                model_name: modelInfo.modelName,
                model_version: modelInfo.modelVersion,
                image_name: imageName
            };
            
            if (baseImage) {
                requestData.base_image = baseImage;
            }
            
            // 发起构建请求（移除重复的alert）
            fetch('/api/2.0/mlflow/models/build-docker', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(requestData)
            })
            .then(response => response.json())
            .then(result => {
                if (result.status === 'success') {
                    alert('🎉 Docker镜像构建已启动！\\n\\n📋 构建信息：\\n• 镜像名称: ' + result.image_name + '\\n• 构建ID: ' + result.build_id + '\\n\\n💡 请查看服务器日志了解构建进度');
                } else {
                    alert('❌ 构建失败: ' + result.message);
                }
            })
            .catch(error => {
                console.error('Docker构建请求失败:', error);
                alert('❌ 构建请求失败: ' + error.message);
            });
        };
        
        // 初始化
        function init() {

            
            // 绑定按钮事件
            const buildBtn = document.getElementById('mlflow-docker-build-btn');
            if (buildBtn) {
                buildBtn.addEventListener('click', showBuildDialog);

            }
            
            // 监听路由变化 - 多种事件确保覆盖所有情况
            window.addEventListener('hashchange', showDockerBuildButton);
            window.addEventListener('popstate', showDockerBuildButton);
            
            // 监听React路由变化（如果可用）
            if (window.history && window.history.pushState) {
                const originalPushState = window.history.pushState;
                window.history.pushState = function() {
                    originalPushState.apply(window.history, arguments);
                    setTimeout(showDockerBuildButton, 100);
                };
            }
            
            // 立即检查一次
            showDockerBuildButton();
            
            // 然后延迟检查确保React应用完全加载
            setTimeout(function() {
                showDockerBuildButton();
            }, 500);
            
            // 移除定期检查 - hashchange事件已足够处理路由变化
        }
        
        // 智能初始化：等待DOM和React应用都准备好
        function smartInit() {
            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', function() {
                    // DOM加载完成后，等待React应用渲染
                    waitForReactApp();
                });
            } else {
                waitForReactApp();
            }
        }
        
        function waitForReactApp() {
            // 检查React根元素是否存在且有内容
            const reactRoot = document.getElementById('root');
            if (reactRoot && reactRoot.children.length > 0) {
                init();
            } else {
                // React还没准备好，短暂延迟后重试
                setTimeout(waitForReactApp, 100);
            }
        }
        
        smartInit();
    })();
    </script>
    '''


def _find_app(app_name: str) -> str:
    apps = get_entry_points("mlflow.app")
    for app in apps:
        if app.name == app_name:
            return app.value

    raise MlflowException(
        f"Failed to find app '{app_name}'. Available apps: {[a.name for a in apps]}"
    )


def _is_factory(app: str) -> bool:
    """
    Returns True if the given app is a factory function, False otherwise.

    Args:
        app: The app to check, e.g. "mlflow.server.app:app
    """
    module, obj_name = app.rsplit(":", 1)
    mod = importlib.import_module(module)
    obj = getattr(mod, obj_name)
    return isinstance(obj, types.FunctionType)


def get_app_client(app_name: str, *args, **kwargs):
    """
    Instantiate a client provided by an app.

    Args:
        app_name: The app name defined in `setup.py`, e.g., "basic-auth".
        args: Additional arguments passed to the app client constructor.
        kwargs: Additional keyword arguments passed to the app client constructor.

    Returns:
        An app client instance.
    """
    clients = get_entry_points("mlflow.app.client")
    for client in clients:
        if client.name == app_name:
            cls = client.load()
            return cls(*args, **kwargs)

    raise MlflowException(
        f"Failed to find client for '{app_name}'. Available clients: {[c.name for c in clients]}"
    )


def _build_waitress_command(waitress_opts, host, port, app_name, is_factory):
    opts = shlex.split(waitress_opts) if waitress_opts else []
    return [
        sys.executable,
        "-m",
        "waitress",
        *opts,
        f"--host={host}",
        f"--port={port}",
        "--ident=mlflow",
        *(["--call"] if is_factory else []),
        app_name,
    ]


def _build_gunicorn_command(gunicorn_opts, host, port, workers, app_name):
    bind_address = f"{host}:{port}"
    opts = shlex.split(gunicorn_opts) if gunicorn_opts else []
    return [
        sys.executable,
        "-m",
        "gunicorn",
        *opts,
        "-b",
        bind_address,
        "-w",
        str(workers),
        app_name,
    ]


def _run_server(
    file_store_path,
    registry_store_uri,
    default_artifact_root,
    serve_artifacts,
    artifacts_only,
    artifacts_destination,
    host,
    port,
    static_prefix=None,
    workers=None,
    gunicorn_opts=None,
    waitress_opts=None,
    expose_prometheus=None,
    app_name=None,
):
    """
    Run the MLflow server, wrapping it in gunicorn or waitress on windows

    Args:
        static_prefix: If set, the index.html asset will be served from the path static_prefix.
                       If left None, the index.html asset will be served from the root path.

    Returns:
        None
    """
    env_map = {}
    if file_store_path:
        env_map[BACKEND_STORE_URI_ENV_VAR] = file_store_path
    if registry_store_uri:
        env_map[REGISTRY_STORE_URI_ENV_VAR] = registry_store_uri
    if default_artifact_root:
        env_map[ARTIFACT_ROOT_ENV_VAR] = default_artifact_root
    if serve_artifacts:
        env_map[SERVE_ARTIFACTS_ENV_VAR] = "true"
    if artifacts_only:
        env_map[ARTIFACTS_ONLY_ENV_VAR] = "true"
    if artifacts_destination:
        env_map[ARTIFACTS_DESTINATION_ENV_VAR] = artifacts_destination
    if static_prefix:
        env_map[STATIC_PREFIX_ENV_VAR] = static_prefix

    if expose_prometheus:
        env_map[PROMETHEUS_EXPORTER_ENV_VAR] = expose_prometheus

    secret_key = MLFLOW_FLASK_SERVER_SECRET_KEY.get()
    if secret_key:
        env_map[MLFLOW_FLASK_SERVER_SECRET_KEY.name] = secret_key

    if app_name is None:
        app = f"{__name__}:app"
        is_factory = False
    else:
        app = _find_app(app_name)
        is_factory = _is_factory(app)
        # `waitress` doesn't support `()` syntax for factory functions.
        # Instead, we need to use the `--call` flag.
        app = f"{app}()" if (not is_windows() and is_factory) else app

    # TODO: eventually may want waitress on non-win32
    if sys.platform == "win32":
        full_command = _build_waitress_command(waitress_opts, host, port, app, is_factory)
    else:
        full_command = _build_gunicorn_command(gunicorn_opts, host, port, workers or 4, app)
    _exec_cmd(full_command, extra_env=env_map, capture_output=False)
