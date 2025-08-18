# MLflow 认证系统部署指南

## 修改文件清单

### 必须复制的核心文件

#### 1. 服务器核心文件
```
server/__init__.py              # 添加了前端组件注入功能
server/auth/__init__.py         # 修改了认证逻辑和登录页面
```

#### 2. 认证系统完整目录
```
server/auth/                    # 整个认证系统目录
├── __init__.py                # 主要认证逻辑（已修改）
├── basic_auth.ini             # 配置文件
├── config.py                  # 配置读取
├── logo.py                    # MLflow Logo
├── permissions.py             # 权限定义
├── routes.py                  # 路由定义
├── sqlalchemy_store.py        # 数据访问层
└── db/                        # 数据库相关
    ├── models.py              # 数据模型
    └── migrations/            # 数据库迁移
```

### 可选文件（文档）

#### 3. 文档文件（根目录下）
```
comprehensive_auth_upgrade_plan.md                    # 升级方案
UPGRADE_IMPLEMENTATION_SUMMARY.md                     # 实施总结  
MLFLOW_FRONTEND_COMPONENTS_IMPLEMENTATION.md          # 前端组件文档
MLFLOW_AUTHENTICATION_SYSTEM_COMPLETE_GUIDE.md       # 综合指南
DEPLOYMENT_GUIDE.md                                   # 本部署指南
```

## 部署方法

### 方法一：完整目录替换（推荐）

1. **备份目标机器的MLflow**
   ```bash
   cp -r /path/to/anaconda/lib/python3.x/site-packages/mlflow /path/to/backup/mlflow_backup
   ```

2. **复制修改后的完整MLflow目录**
   ```bash
   # 将当前机器的整个mlflow目录复制到目标机器
   scp -r /path/to/current/mlflow user@target:/path/to/anaconda/lib/python3.x/site-packages/
   ```

### 方法二：选择性文件替换

1. **只复制修改的核心文件**
   ```bash
   # 复制修改的服务器文件
   scp server/__init__.py user@target:/path/to/mlflow/server/
   scp server/auth/__init__.py user@target:/path/to/mlflow/server/auth/
   
   # 或者复制整个认证目录（如果有其他修改）
   scp -r server/auth/ user@target:/path/to/mlflow/server/
   ```

2. **可选：复制文档文件**
   ```bash
   scp *.md user@target:/path/to/mlflow/
   ```

### 方法三：打包部署

1. **创建部署包**
   ```bash
   # 在当前机器创建部署包
   tar -czf mlflow_auth_upgrade.tar.gz \
       server/__init__.py \
       server/auth/ \
       *.md
   ```

2. **在目标机器解压**
   ```bash
   # 传输到目标机器
   scp mlflow_auth_upgrade.tar.gz user@target:/tmp/
   
   # 在目标机器解压
   cd /path/to/anaconda/lib/python3.x/site-packages/mlflow/
   tar -xzf /tmp/mlflow_auth_upgrade.tar.gz
   ```

## 部署后配置

### 1. 安装依赖
```bash
pip install Flask-Session
```

### 2. 设置环境变量
```bash
export MLFLOW_FLASK_SERVER_SECRET_KEY="your-secret-key-here"
```

### 3. 数据库初始化
```bash
# 启动MLflow服务器（会自动初始化数据库）
mlflow server --app-name basic-auth
```

## 验证部署

### 1. 启动服务
```bash
mlflow server --app-name basic-auth --host 0.0.0.0 --port 5000
```

### 2. 检查功能
- 访问 `http://localhost:5000` 
- 应该自动重定向到 `/login`
- 登录后应该在左下角看到导航组件
- 管理员用户应该能看到"管理面板"按钮

### 3. 测试清单
- [ ] 登录功能正常
- [ ] 左下角显示导航组件
- [ ] 登出按钮工作正常
- [ ] 管理员可以访问 `/admin`
- [ ] 普通用户无法访问管理功能

## 注意事项

### 1. 权限设置
确保MLflow目录的权限正确：
```bash
chmod -R 755 /path/to/mlflow/server/
```

### 2. Python版本兼容性
确保目标机器的Python版本与源机器兼容。

### 3. 数据库路径
如果使用SQLite，确保数据库文件路径正确配置在 `basic_auth.ini` 中。

### 4. 防火墙设置
确保目标机器的防火墙允许MLflow端口访问。

## 故障排除

### 常见问题

1. **导入错误**
   ```
   ImportError: cannot import name '_get_current_user_session'
   ```
   **解决**: 确保 `server/auth/__init__.py` 文件已正确复制

2. **前端组件不显示**
   ```
   检查 server/__init__.py 是否包含组件注入代码
   ```

3. **认证失败**
   ```
   检查 basic_auth.ini 配置是否正确
   检查数据库是否已初始化
   ```

4. **权限错误**
   ```
   检查文件权限和目录权限
   确保MLflow进程有读写权限
   ```

## 回滚方案

如果部署出现问题，可以快速回滚：

1. **使用备份恢复**
   ```bash
   rm -rf /path/to/mlflow/
   cp -r /path/to/backup/mlflow_backup /path/to/mlflow/
   ```

2. **重新安装MLflow**
   ```bash
   pip uninstall mlflow
   pip install mlflow
   ```

## 总结

最简单的部署方法是**方法一：完整目录替换**，这样可以确保所有修改都被正确复制，避免遗漏任何文件。

核心修改只涉及两个文件：
- `server/__init__.py`
- `server/auth/__init__.py`

但为了确保完整性，建议复制整个 `server/auth/` 目录和修改后的 `server/__init__.py` 文件。
