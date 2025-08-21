from mlflow.server.handlers import _get_rest_path

HOME = "/"
LOGIN = "/login"
LOGOUT = "/logout"
SIGNUP = "/signup"
ADMIN = "/admin"
ADMIN_USERS = "/admin/users"
ADMIN_DELETE_USER = "/admin/users/<user_id>/delete"
ADMIN_EXPERIMENTS = "/admin/experiments"
ADMIN_EXPERIMENT_PERMISSION = "/admin/experiments/<experiment_id>"
ADMIN_CREATE_EXPERIMENT = "/admin/experiments/create"
ADMIN_DELETE_EXPERIMENT = "/admin/experiments/<experiment_id>/delete"
CREATE_USER = _get_rest_path("/mlflow/users/create")
CREATE_USER_UI = _get_rest_path("/mlflow/users/create-ui")
GET_USER = _get_rest_path("/mlflow/users/get")
UPDATE_USER_PASSWORD = _get_rest_path("/mlflow/users/update-password")
UPDATE_USER_ADMIN = _get_rest_path("/mlflow/users/update-admin")
DELETE_USER = _get_rest_path("/mlflow/users/delete")
CREATE_EXPERIMENT_PERMISSION = _get_rest_path("/mlflow/experiments/permissions/create")
GET_EXPERIMENT_PERMISSION = _get_rest_path("/mlflow/experiments/permissions/get")
UPDATE_EXPERIMENT_PERMISSION = _get_rest_path("/mlflow/experiments/permissions/update")
DELETE_EXPERIMENT_PERMISSION = _get_rest_path("/mlflow/experiments/permissions/delete")
CREATE_REGISTERED_MODEL_PERMISSION = _get_rest_path("/mlflow/registered-models/permissions/create")
GET_REGISTERED_MODEL_PERMISSION = _get_rest_path("/mlflow/registered-models/permissions/get")
UPDATE_REGISTERED_MODEL_PERMISSION = _get_rest_path("/mlflow/registered-models/permissions/update")
DELETE_REGISTERED_MODEL_PERMISSION = _get_rest_path("/mlflow/registered-models/permissions/delete")
BUILD_MODEL_DOCKER = _get_rest_path("/mlflow/models/build-docker")
