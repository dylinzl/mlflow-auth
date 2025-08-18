
# 综合性认证授权系统升级方案 for MLflow

## 1. 方案目标与概述

本文档旨在提供一个完整的、端到端的实施方案，用于将 MLflow 当前的 `basic-auth` 模块从一个基础的 HTTP Basic Authentication 系统，全面升级为一个功能强大的、有状态的、基于会话的认证授权平台。

### 1.1. 待实现的核心功能

1.  **现代化会话认证**:
    *   废弃浏览器原生登录弹窗，采用**基于表单的登录页面** (`/login`)。
    *   实现**用户主动登出** (`/logout`) 功能。
    *   后端采用 **Cookie-Session** 机制来管理用户登录状态。

2.  **会话生命周期管理**:
    *   实现**会话超时强制登出**机制。具体需求为：用户登录后，会话最长有效期为一天 (24小时)，到期后必须重新登录。

3.  **可视化管理员面板**:
    *   创建一个管理员专属的前端管理界面，入口为 (`/admin`)。
    *   **用户管理**: 在UI上可视化地**展示用户列表**，并能**创建新用户**（包括指定其是否为管理员）。
    *   **权限管理**: 在UI上可视化地**展示实验列表**，并能为任何用户**分配/修改其在任意实验中的权限**（READ, EDIT, MANAGE等）。

### 1.2. 设计哲学

*   **最小化侵入**: 尽可能复用原有的 `sqlalchemy_store` 数据访问层和 `permissions.py` 中的权限定义。
*   **逻辑分离**: 将认证 (Authentication) 和授权 (Authorization) 的修改解耦。新的会话机制将主要替换认证部分，而大部分授权逻辑保持不变。
*   **渐进式增强**: 方案步骤清晰，可分阶段实施，首先构建会话基础，然后在其上添加管理面板。

---

## 2. 实施步骤详解

### 第 1 阶段：奠定基础 - 迁移到会话认证并配置超时

此阶段是所有后续功能的前提。

#### 步骤 1.1: 引入依赖并升级数据库模型

1.  **安装新库**: 在项目环境中安装 `Flask-Session`。
    ```bash
    pip install Flask-Session
    ```
2.  **定义 Session 表**: 我们需要一个新表来存储会话数据。
    *   **修改文件**: `mlflow/server/auth/db/models.py`
    *   **新增内容**: 在文件末尾添加 `SqlSession` 模型定义。
        ```python
        # (在文件末尾新增)
        from sqlalchemy import LargeBinary, DateTime
        
        class SqlSession(Base):
            __tablename__ = "sessions"
            id = Column(Integer, primary_key=True)
            session_id = Column(String(255), unique=True)
            data = Column(LargeBinary)
            expiry = Column(DateTime)
        ```
3.  **数据库迁移**: 需要创建一个 Alembic 迁移脚本来在数据库中物理创建这张表。
    *   **操作（示意）**: 运行 `alembic revision -m "add sessions table"` 并编辑生成的脚本，确保 `upgrade` 函数包含创建 `sessions` 表的逻辑。

#### 步骤 1.2: 更新系统配置

1.  **修改配置文件**:
    *   **文件**: `mlflow/server/auth/basic_auth.ini`
    *   **修改内容**:
        ```ini
        # ... [database] section ...

        # 新增 [session] section
        [session]
        SESSION_TYPE = sqlalchemy
        SESSION_PERMANENT = True
        # 设置会话有效期为 1 天 (24 * 60 * 60 = 86400 秒)
        PERMANENT_SESSION_LIFETIME = 86400
        SESSION_COOKIE_SAMESITE = None

        [auth]
        # ...
        # 关键: 切换认证函数为新的会话认证函数
        authorization_function = mlflow.server.auth:authenticate_request_session
        ```
2.  **修改配置读取逻辑**:
    *   **文件**: `mlflow/server/auth/config.py`
    *   **修改内容**: 让 `read_auth_config` 函数能够解析和返回 `[session]` 配置。

#### 步骤 1.3: 重构核心认证逻辑

1.  **修改文件**: `mlflow/server/auth/__init__.py`
2.  **主要改动**:
    *   **引入新模块**: `from flask import session, redirect, url_for, flash` 和 `from flask_session import Session`。
    *   **创建新的认证函数 `authenticate_request_session`**:
        *   此函数将成为新的认证核心。它的逻辑是：检查 `flask.session` 中是否存在 `username`。
        *   如果存在，说明用户已登录，认证成功。为了兼容旧的授权逻辑，它依然需要返回一个 `Authorization` 对象。
        *   如果不存在，判断请求类型。若是浏览器HTML请求，则重定向到 `/login`；若是API请求，则返回 `401 Unauthorized`。
    *   **创建登录/登出路由和视图**:
        *   `GET /login`: 显示一个包含 `username`, `password` 输入框的 HTML 登录表单。
        *   `POST /login`: 处理表单提交。调用 `store.authenticate_user` 验证凭证。成功后，将用户信息（如 `username`, `user_id`）写入 `session`，并重定向到用户之前想访问的页面或主页。失败则重回登录页并显示错误提示。
        *   `GET /logout`: 清除 `session` (`session.clear()`)，然后重定向到登录页面。
    *   **在 `create_app` 中集成**:
        *   初始化 `Flask-Session`：`Session(app)`。
        *   将 `app.config` 更新为从 `session_config` 读取的配置。
        *   使用 `app.add_url_rule` 注册 `/login`, `/logout` 等新路由。

---

### 第 2 阶段：构建功能强大的管理员面板

在会话认证的基础上，我们为管理员构建专属的可视化管理工具。

#### 步骤 2.1: 设计管理员路由保护机制

1.  **创建守护装饰器**: 在 `__init__.py` 中创建一个名为 `@admin_panel_guard` 的 Python 装饰器。
    *   **功能**: 任何被此装饰器修饰的路由视图，在执行前都会先调用 `sender_is_admin()` 进行检查。如果检查失败，则立即返回 403 Forbidden 页面，有效保护所有管理员功能。

#### 步骤 2.2: 设计面板UI和后端逻辑

所有新路由和视图逻辑都在 `__init__.py` 中实现，并使用 `@admin_panel_guard` 保护。

1.  **管理面板主页 (`GET /admin`)**:
    *   **后端**: 创建一个 `admin_dashboard` 视图函数。它可以调用 `store.list_users()` 等来获取一些基本统计数据。
    *   **前端**: 渲染一个简单的HTML页面，显示欢迎信息、统计数据，并提供导航链接到“用户管理”和“实验权限管理”。

2.  **用户管理 (`GET /admin/users`, `POST /admin/users/create`)**:
    *   **后端 (`GET`)**: 创建 `admin_users_page` 视图。它调用 `store.list_users()` 获取所有用户数据，并传递给前端模板。
    *   **后端 (`POST`)**: 创建 `admin_create_user` 视图。它从表单中获取 `username`, `password`, `is_admin`，然后调用 `store.create_user()` 创建新用户，最后重定向回用户列表页。
    *   **前端**: 渲染一个包含两部分的HTML页面：
        a.  一个 `<table>`，遍历并展示所有用户及其管理员状态。
        b.  一个 `<form>`，用于输入新用户的各项信息并提交。

3.  **实验权限管理**:
    *   **实验列表页 (`GET /admin/experiments`)**:
        *   **后端**: 创建 `admin_experiments_page` 视图。它调用 `_get_tracking_store().search_experiments()` 获取所有实验的列表。
        *   **前端**: 渲染一个 `<table>`，列出所有实验的ID和名称，并在每行提供一个“管理权限”的链接，指向该实验的专属权限页面（如 `/admin/experiments/123`）。
    *   **单个实验权限配置页 (`GET /admin/experiments/<exp_id>`)**:
        *   **后端**: 创建 `admin_manage_experiment_permission_page` 视图。这是最核心的页面，它需要：
            1.  调用 `store.list_users()` 获取所有用户。
            2.  调用一个**需要新增**的 `store.get_all_permissions_for_experiment(exp_id)` 函数，获取当前实验已分配的所有权限记录。
            3.  将两份数据整合后传递给模板。
        *   **前端**: 渲染一个 `<table>`。
            *   **行**: 每个用户占一行。
            *   **列**: 用户名 | 当前权限 | 新权限操作。
            *   “新权限操作”列是一个独立的 `<form>`，包含一个隐藏的 `username` 字段，一个包含所有可选权限（从 `permissions.py` 中动态获取）的下拉菜单 `<select>`，以及一个“更新”按钮。
    *   **权限更新逻辑 (`POST /admin/experiments/<exp_id>/update`)**:
        *   **后端**: 创建 `admin_update_experiment_permission` 视图。它从表单中获取 `username` 和新的 `permission`，然后调用一个**需要新增**的 `store.update_or_create_experiment_permission(...)` 便捷函数来更新数据库，最后重定向回单个实验的权限配置页。

#### 步骤 2.3: 扩展数据访问层

为支持管理员面板的复杂查询，需要对 `sqlalchemy_store.py` 进行增强。

1.  **新增 `get_all_permissions_for_experiment(experiment_id)`**:
    *   此函数需要执行一个 `JOIN` 查询，联合 `users` 表和 `experiment_permissions` 表，一次性地、高效地拉取某个实验的所有权限分配情况（谁有什么权限）。
2.  **新增 `update_or_create_experiment_permission(...)`**:
    *   这个函数会尝试根据 `(experiment_id, user_id)` 查询权限记录。如果存在，则 `UPDATE` 其 `permission` 字段；如果不存在，则 `INSERT` 一条新记录。这能用一个函数调用处理两种情况，简化后端逻辑。

## 3. 最终用户流程

*   **普通用户**: 访问 MLflow -> 被重定向到 `/login` -> 登录 -> 使用应用 -> 24小时后或点击 `/logout` 后会话失效，需要重新登录。
*   **管理员**: 登录流程同上 -> 访问 `/admin` -> 进入管理面板 -> 点击“用户管理”或“实验权限管理”进行操作 -> 所有操作都在一个直观的UI中完成。

该综合方案将 MLflow 的认证授权能力提升到了一个现代化、企业级的水平，显著增强了其安全性、易用性和管理效率。
