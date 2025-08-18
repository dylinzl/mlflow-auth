"""
Usage
-----

.. code-block:: bash

    mlflow server --app-name basic-auth
"""

import functools
import importlib
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Callable

import sqlalchemy
from flask import (
    Flask,
    Request,
    Response,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
)
from werkzeug.datastructures import Authorization

from mlflow import MlflowException
from mlflow.entities import Experiment
from mlflow.entities.logged_model import LoggedModel
from mlflow.entities.model_registry import RegisteredModel
from mlflow.environment_variables import MLFLOW_FLASK_SERVER_SECRET_KEY
from mlflow.protos.databricks_pb2 import (
    BAD_REQUEST,
    INTERNAL_ERROR,
    INVALID_PARAMETER_VALUE,
    RESOURCE_DOES_NOT_EXIST,
    ErrorCode,
)
from mlflow.protos.model_registry_pb2 import (
    CreateModelVersion,
    CreateRegisteredModel,
    DeleteModelVersion,
    DeleteModelVersionTag,
    DeleteRegisteredModel,
    DeleteRegisteredModelAlias,
    DeleteRegisteredModelTag,
    GetLatestVersions,
    GetModelVersion,
    GetModelVersionByAlias,
    GetModelVersionDownloadUri,
    GetRegisteredModel,
    RenameRegisteredModel,
    SearchRegisteredModels,
    SetModelVersionTag,
    SetRegisteredModelAlias,
    SetRegisteredModelTag,
    TransitionModelVersionStage,
    UpdateModelVersion,
    UpdateRegisteredModel,
)
from mlflow.protos.service_pb2 import (
    CreateExperiment,
    # Routes for logged models
    CreateLoggedModel,
    CreateRun,
    DeleteExperiment,
    DeleteExperimentTag,
    DeleteLoggedModel,
    DeleteLoggedModelTag,
    DeleteRun,
    DeleteTag,
    FinalizeLoggedModel,
    GetExperiment,
    GetExperimentByName,
    GetLoggedModel,
    GetMetricHistory,
    GetRun,
    ListArtifacts,
    LogBatch,
    LogLoggedModelParamsRequest,
    LogMetric,
    LogModel,
    LogParam,
    RestoreExperiment,
    RestoreRun,
    SearchExperiments,
    SearchLoggedModels,
    SetExperimentTag,
    SetLoggedModelTags,
    SetTag,
    UpdateExperiment,
    UpdateRun,
)
from mlflow.server import app as base_app
from mlflow.server.auth.config import read_auth_config
from mlflow.server.auth.logo import MLFLOW_LOGO
from mlflow.server.auth.permissions import MANAGE, Permission, get_permission
from mlflow.server.auth.routes import (
    ADMIN,
    ADMIN_CREATE_EXPERIMENT,
    ADMIN_DELETE_EXPERIMENT,
    ADMIN_DELETE_USER,
    ADMIN_EXPERIMENT_PERMISSION,
    ADMIN_EXPERIMENTS,
    ADMIN_USERS,
    CREATE_EXPERIMENT_PERMISSION,
    CREATE_REGISTERED_MODEL_PERMISSION,
    CREATE_USER,
    CREATE_USER_UI,
    DELETE_EXPERIMENT_PERMISSION,
    DELETE_REGISTERED_MODEL_PERMISSION,
    DELETE_USER,
    GET_EXPERIMENT_PERMISSION,
    GET_REGISTERED_MODEL_PERMISSION,
    GET_USER,
    HOME,
    LOGIN,
    LOGOUT,
    SIGNUP,
    UPDATE_EXPERIMENT_PERMISSION,
    UPDATE_REGISTERED_MODEL_PERMISSION,
    UPDATE_USER_ADMIN,
    UPDATE_USER_PASSWORD,
)
from mlflow.server.auth.sqlalchemy_store import SqlAlchemyStore
from mlflow.server.handlers import (
    _get_model_registry_store,
    _get_request_message,
    _get_tracking_store,
    catch_mlflow_exception,
    get_endpoints,
)
from mlflow.store.entities import PagedList
from mlflow.utils.proto_json_utils import message_to_json, parse_dict
from mlflow.utils.rest_utils import _REST_API_PATH_PREFIX
from mlflow.utils.search_utils import SearchUtils

try:
    from flask_wtf.csrf import CSRFProtect
except ImportError as e:
    raise ImportError(
        "The MLflow basic auth app requires the Flask-WTF package to perform CSRF "
        "validation. Please run `pip install mlflow[auth]` to install it."
    ) from e

try:
    from flask_session import Session
except ImportError as e:
    raise ImportError(
        "The MLflow session auth app requires the Flask-Session package to manage "
        "sessions. Please run `pip install Flask-Session` to install it."
    ) from e

# WSGI to ASGI adapter for uvicorn compatibility
try:
    from asgiref.wsgi import WsgiToAsgi
    ASGI_AVAILABLE = True
except ImportError:
    ASGI_AVAILABLE = False

_logger = logging.getLogger(__name__)

auth_config = read_auth_config()
store = SqlAlchemyStore()


def is_unprotected_route(path: str) -> bool:
    return path.startswith(("/static", "/favicon.ico", "/health")) or path in (LOGIN, SIGNUP)


def make_basic_auth_response() -> Response:
    """Legacy function - now returns session-based auth response to avoid browser popup"""
    res = make_response(
        "You are not authenticated. Please login at /login to access this resource."
    )
    res.status_code = 401
    # Remove WWW-Authenticate header to prevent browser popup
    return res


def make_forbidden_response() -> Response:
    res = make_response("Permission denied")
    res.status_code = 403
    return res


def _get_request_param(param: str) -> str:
    if request.method == "GET":
        args = request.args
    elif request.method in ("POST", "PATCH"):
        args = request.json
    elif request.method == "DELETE":
        args = request.json if request.is_json else request.args
    else:
        raise MlflowException(
            f"Unsupported HTTP method '{request.method}'",
            BAD_REQUEST,
        )

    args = args | (request.view_args or {})
    if param not in args:
        # Special handling for run_id
        if param == "run_id":
            return _get_request_param("run_uuid")
        raise MlflowException(
            f"Missing value for required parameter '{param}'. "
            "See the API docs for more information about request parameters.",
            INVALID_PARAMETER_VALUE,
        )
    return args[param]


def _get_permission_from_store_or_default(store_permission_func: Callable[[], str]) -> Permission:
    """
    Attempts to get permission from store,
    and returns default permission if no record is found.
    """
    try:
        perm = store_permission_func()
    except MlflowException as e:
        if e.error_code == ErrorCode.Name(RESOURCE_DOES_NOT_EXIST):
            perm = auth_config.default_permission
        else:
            raise
    return get_permission(perm)


def _get_permission_from_experiment_id() -> Permission:
    experiment_id = _get_request_param("experiment_id")
    username = _get_username_from_auth(authenticate_request())
    return _get_permission_from_store_or_default(
        lambda: store.get_experiment_permission(experiment_id, username).permission
    )


_EXPERIMENT_ID_PATTERN = re.compile(r"^(\d+)/")


def _get_experiment_id_from_view_args():
    if artifact_path := request.view_args.get("artifact_path"):
        if m := _EXPERIMENT_ID_PATTERN.match(artifact_path):
            return m.group(1)
    return None


def _get_permission_from_experiment_id_artifact_proxy() -> Permission:
    if experiment_id := _get_experiment_id_from_view_args():
        username = _get_username_from_auth(authenticate_request())
        return _get_permission_from_store_or_default(
            lambda: store.get_experiment_permission(experiment_id, username).permission
        )
    return get_permission(auth_config.default_permission)


def _get_permission_from_experiment_name() -> Permission:
    experiment_name = _get_request_param("experiment_name")
    store_exp = _get_tracking_store().get_experiment_by_name(experiment_name)
    if store_exp is None:
        raise MlflowException(
            f"Could not find experiment with name {experiment_name}",
            error_code=RESOURCE_DOES_NOT_EXIST,
        )
    username = _get_username_from_auth(authenticate_request())
    return _get_permission_from_store_or_default(
        lambda: store.get_experiment_permission(store_exp.experiment_id, username).permission
    )


def _get_permission_from_run_id() -> Permission:
    # run permissions inherit from parent resource (experiment)
    # so we just get the experiment permission
    run_id = _get_request_param("run_id")
    run = _get_tracking_store().get_run(run_id)
    experiment_id = run.info.experiment_id
    username = _get_username_from_auth(authenticate_request())
    return _get_permission_from_store_or_default(
        lambda: store.get_experiment_permission(experiment_id, username).permission
    )


def _get_permission_from_model_id() -> Permission:
    # logged model permissions inherit from parent resource (experiment)
    model_id = _get_request_param("model_id")
    model = _get_tracking_store().get_logged_model(model_id)
    experiment_id = model.experiment_id
    username = _get_username_from_auth(authenticate_request())
    return _get_permission_from_store_or_default(
        lambda: store.get_experiment_permission(experiment_id, username).permission
    )


def _get_permission_from_registered_model_name() -> Permission:
    name = _get_request_param("name")
    username = _get_username_from_auth(authenticate_request())
    return _get_permission_from_store_or_default(
        lambda: store.get_registered_model_permission(name, username).permission
    )


def validate_can_read_experiment():
    return _get_permission_from_experiment_id().can_read


def validate_can_read_experiment_by_name():
    return _get_permission_from_experiment_name().can_read


def validate_can_update_experiment():
    return _get_permission_from_experiment_id().can_update


def validate_can_delete_experiment():
    return _get_permission_from_experiment_id().can_delete


def validate_can_manage_experiment():
    return _get_permission_from_experiment_id().can_manage


def validate_can_read_experiment_artifact_proxy():
    return _get_permission_from_experiment_id_artifact_proxy().can_read


def validate_can_update_experiment_artifact_proxy():
    return _get_permission_from_experiment_id_artifact_proxy().can_update


def validate_can_delete_experiment_artifact_proxy():
    return _get_permission_from_experiment_id_artifact_proxy().can_manage


# Runs
def validate_can_read_run():
    return _get_permission_from_run_id().can_read


def validate_can_update_run():
    return _get_permission_from_run_id().can_update


def validate_can_delete_run():
    return _get_permission_from_run_id().can_delete


def validate_can_manage_run():
    return _get_permission_from_run_id().can_manage


# Logged models
def validate_can_read_logged_model():
    return _get_permission_from_model_id().can_read


def validate_can_update_logged_model():
    return _get_permission_from_model_id().can_update


def validate_can_delete_logged_model():
    return _get_permission_from_model_id().can_delete


def validate_can_manage_logged_model():
    return _get_permission_from_model_id().can_manage


# Registered models
def validate_can_read_registered_model():
    return _get_permission_from_registered_model_name().can_read


def validate_can_update_registered_model():
    return _get_permission_from_registered_model_name().can_update


def validate_can_delete_registered_model():
    return _get_permission_from_registered_model_name().can_delete


def validate_can_manage_registered_model():
    return _get_permission_from_registered_model_name().can_manage


def _get_username_from_auth(auth):
    """Extract username from Authorization object (handles both basic and session auth)"""
    if hasattr(auth, 'username'):
        # Basic auth
        return auth.username
    elif hasattr(auth, 'data') and isinstance(auth.data, dict):
        # Session/JWT auth with data dictionary
        return auth.data.get('username')
    else:
        raise MlflowException("Unable to extract username from authorization", INTERNAL_ERROR)


def sender_is_admin():
    """Validate if the sender is admin"""
    auth = authenticate_request()
    username = _get_username_from_auth(auth)
    return store.get_user(username).is_admin


def username_is_sender():
    """Validate if the request username is the sender"""
    username = _get_request_param("username")
    sender = _get_username_from_auth(authenticate_request())
    return username == sender


def validate_can_read_user():
    return username_is_sender()


def validate_can_create_user():
    # only admins can create user, but admins won't reach this validator
    return False


def validate_can_update_user_password():
    return username_is_sender()


def validate_can_update_user_admin():
    # only admins can update, but admins won't reach this validator
    return False


def validate_can_delete_user():
    # only admins can delete, but admins won't reach this validator
    return False


BEFORE_REQUEST_HANDLERS = {
    # Routes for experiments
    GetExperiment: validate_can_read_experiment,
    GetExperimentByName: validate_can_read_experiment_by_name,
    DeleteExperiment: validate_can_delete_experiment,
    RestoreExperiment: validate_can_delete_experiment,
    UpdateExperiment: validate_can_update_experiment,
    SetExperimentTag: validate_can_update_experiment,
    DeleteExperimentTag: validate_can_update_experiment,
    # Routes for runs
    CreateRun: validate_can_update_experiment,
    GetRun: validate_can_read_run,
    DeleteRun: validate_can_delete_run,
    RestoreRun: validate_can_delete_run,
    UpdateRun: validate_can_update_run,
    LogMetric: validate_can_update_run,
    LogBatch: validate_can_update_run,
    LogModel: validate_can_update_run,
    SetTag: validate_can_update_run,
    DeleteTag: validate_can_update_run,
    LogParam: validate_can_update_run,
    GetMetricHistory: validate_can_read_run,
    ListArtifacts: validate_can_read_run,
    # Routes for model registry
    GetRegisteredModel: validate_can_read_registered_model,
    DeleteRegisteredModel: validate_can_delete_registered_model,
    UpdateRegisteredModel: validate_can_update_registered_model,
    RenameRegisteredModel: validate_can_update_registered_model,
    GetLatestVersions: validate_can_read_registered_model,
    CreateModelVersion: validate_can_update_registered_model,
    GetModelVersion: validate_can_read_registered_model,
    DeleteModelVersion: validate_can_delete_registered_model,
    UpdateModelVersion: validate_can_update_registered_model,
    TransitionModelVersionStage: validate_can_update_registered_model,
    GetModelVersionDownloadUri: validate_can_read_registered_model,
    SetRegisteredModelTag: validate_can_update_registered_model,
    DeleteRegisteredModelTag: validate_can_update_registered_model,
    SetModelVersionTag: validate_can_update_registered_model,
    DeleteModelVersionTag: validate_can_delete_registered_model,
    SetRegisteredModelAlias: validate_can_update_registered_model,
    DeleteRegisteredModelAlias: validate_can_delete_registered_model,
    GetModelVersionByAlias: validate_can_read_registered_model,
}


def get_before_request_handler(request_class):
    return BEFORE_REQUEST_HANDLERS.get(request_class)


def _re_compile_path(path: str) -> re.Pattern:
    """
    Convert a path with angle brackets to a regex pattern. For example,
    "/api/2.0/experiments/<experiment_id>" becomes "/api/2.0/experiments/([^/]+)".
    """
    return re.compile(re.sub(r"<([^>]+)>", r"([^/]+)", path))


BEFORE_REQUEST_VALIDATORS = {
    (http_path, method): handler
    for http_path, handler, methods in get_endpoints(get_before_request_handler)
    for method in methods
}

BEFORE_REQUEST_VALIDATORS.update(
    {
        (SIGNUP, "GET"): validate_can_create_user,
        (GET_USER, "GET"): validate_can_read_user,
        (CREATE_USER, "POST"): validate_can_create_user,
        (UPDATE_USER_PASSWORD, "PATCH"): validate_can_update_user_password,
        (UPDATE_USER_ADMIN, "PATCH"): validate_can_update_user_admin,
        (DELETE_USER, "DELETE"): validate_can_delete_user,
        (GET_EXPERIMENT_PERMISSION, "GET"): validate_can_manage_experiment,
        (CREATE_EXPERIMENT_PERMISSION, "POST"): validate_can_manage_experiment,
        (UPDATE_EXPERIMENT_PERMISSION, "PATCH"): validate_can_manage_experiment,
        (DELETE_EXPERIMENT_PERMISSION, "DELETE"): validate_can_manage_experiment,
        (GET_REGISTERED_MODEL_PERMISSION, "GET"): validate_can_manage_registered_model,
        (CREATE_REGISTERED_MODEL_PERMISSION, "POST"): validate_can_manage_registered_model,
        (UPDATE_REGISTERED_MODEL_PERMISSION, "PATCH"): validate_can_manage_registered_model,
        (DELETE_REGISTERED_MODEL_PERMISSION, "DELETE"): validate_can_manage_registered_model,
    }
)


LOGGED_MODEL_BEFORE_REQUEST_HANDLERS = {
    CreateLoggedModel: validate_can_update_experiment,
    GetLoggedModel: validate_can_read_logged_model,
    DeleteLoggedModel: validate_can_delete_logged_model,
    FinalizeLoggedModel: validate_can_update_logged_model,
    DeleteLoggedModelTag: validate_can_delete_logged_model,
    SetLoggedModelTags: validate_can_update_logged_model,
    LogLoggedModelParamsRequest: validate_can_update_logged_model,
}


def get_logged_model_before_request_handler(request_class):
    return LOGGED_MODEL_BEFORE_REQUEST_HANDLERS.get(request_class)


LOGGED_MODEL_BEFORE_REQUEST_VALIDATORS = {
    # Paths for logged models contains path parameters (e.g. /mlflow/logged-models/<model_id>)
    (_re_compile_path(http_path), method): handler
    for http_path, handler, methods in get_endpoints(get_logged_model_before_request_handler)
    for method in methods
}


def _is_proxy_artifact_path(path: str) -> bool:
    return path.startswith(f"{_REST_API_PATH_PREFIX}/mlflow-artifacts/artifacts/")


def _get_proxy_artifact_validator(
    method: str, view_args: dict[str, Any] | None
) -> Callable[[], bool] | None:
    if view_args is None:
        return validate_can_read_experiment_artifact_proxy  # List

    return {
        "GET": validate_can_read_experiment_artifact_proxy,  # Download
        "PUT": validate_can_update_experiment_artifact_proxy,  # Upload
        "DELETE": validate_can_delete_experiment_artifact_proxy,  # Delete
    }.get(method)


def authenticate_request() -> Authorization | Response:
    """Use configured authorization function to get request authorization."""
    auth_func = get_auth_func(auth_config.authorization_function)
    return auth_func()


@functools.lru_cache(maxsize=None)
def get_auth_func(authorization_function: str) -> Callable[[], Authorization | Response]:
    """
    Import and return the specified authorization function.

    Args:
        authorization_function: A string of the form "module.submodule:auth_func"
    """
    mod_name, fn_name = authorization_function.split(":", 1)
    module = importlib.import_module(mod_name)
    return getattr(module, fn_name)


def authenticate_request_basic_auth() -> Authorization | Response:
    """Authenticate the request using basic auth."""
    if request.authorization is None:
        return make_basic_auth_response()

    username = request.authorization.username
    password = request.authorization.password
    if store.authenticate_user(username, password):
        return request.authorization
    else:
        # let user attempt login again
        return make_basic_auth_response()


def authenticate_request_session() -> Authorization | Response:
    """Authenticate the request using session-based auth."""
    # Check if user info exists in session
    if "username" in session and "user_id" in session:
        # Check if session has expired
        if "login_time" in session:
            login_time = datetime.fromisoformat(session["login_time"])
            session_lifetime = timedelta(seconds=auth_config.session_config.get("PERMANENT_SESSION_LIFETIME", 86400))
            if datetime.now() - login_time > session_lifetime:
                # Session expired, clear session
                session.clear()
                return _handle_unauthenticated_request()
        
        # Session is valid, create Authorization object for compatibility
        # Use the same pattern as JWT auth: Authorization(auth_type="session", data=user_info)
        user_data = {
            "username": session["username"],
            "user_id": session["user_id"],
            "is_admin": session.get("is_admin", False)
        }
        return Authorization(auth_type="session", data=user_data)
    else:
        return _handle_unauthenticated_request()


def _get_current_user_session():
    """Get current user session information for UI components."""
    try:
        if "username" in session and "user_id" in session:
            # Check if session has expired
            session_created_at = session.get("created_at")
            if session_created_at:
                created_time = datetime.fromisoformat(session_created_at)
                current_time = datetime.now()
                # Get session lifetime from config, default to 24 hours
                try:
                    session_lifetime = timedelta(seconds=auth_config.session_config.get("PERMANENT_SESSION_LIFETIME", 86400))
                except (NameError, AttributeError):
                    session_lifetime = timedelta(seconds=86400)  # Default 24 hours
                
                if current_time - created_time > session_lifetime:
                    # Session expired
                    return None
            
            # Session is valid, return user info
            return {
                "username": session["username"],
                "user_id": session["user_id"],
                "is_admin": session.get("is_admin", False)
            }
    except Exception:
        # Any error means no valid session
        pass
    return None


def _handle_unauthenticated_request() -> Response:
    """Handle unauthenticated requests"""
    # Check if it's a browser request (HTML request)
    accept_header = request.headers.get("Accept", "")
    if "text/html" in accept_header:
        # Browser request, redirect to login page
        # Save original request URL for redirect after login
        next_url = request.url if request.method == "GET" else None
        login_url = LOGIN
        if next_url:
            login_url += f"?next={next_url}"
        return redirect(login_url)
    else:
        # API request, return 401 without WWW-Authenticate header to avoid browser popup
        res = make_response(
            "You are not authenticated. Please login at /login to access this resource.",
            401
        )
        return res


def _find_validator(req: Request) -> Callable[[], bool] | None:
    """
    Finds the validator matching the request path and method.
    """
    if "/mlflow/logged-models" in req.path:
        # logged model routes are not registered in the app
        # so we need to check them manually
        return next(
            (
                v
                for (pat, method), v in LOGGED_MODEL_BEFORE_REQUEST_VALIDATORS.items()
                if pat.fullmatch(req.path) and method == req.method
            ),
            None,
        )
    else:
        return BEFORE_REQUEST_VALIDATORS.get((req.path, req.method))


@catch_mlflow_exception
def _before_request():
    if is_unprotected_route(request.path):
        return

    authorization = authenticate_request()
    if isinstance(authorization, Response):
        return authorization
    elif not isinstance(authorization, Authorization):
        raise MlflowException(
            f"Unsupported result type from {auth_config.authorization_function}: "
            f"'{type(authorization).__name__}'",
            INTERNAL_ERROR,
        )

    # admins don't need to be authorized
    if sender_is_admin():
        return

    # authorization
    if validator := _find_validator(request):
        if not validator():
            return make_forbidden_response()
    elif _is_proxy_artifact_path(request.path):
        if validator := _get_proxy_artifact_validator(request.method, request.view_args):
            if not validator():
                return make_forbidden_response()


def set_can_manage_experiment_permission(resp: Response):
    response_message = CreateExperiment.Response()
    parse_dict(resp.json, response_message)
    experiment_id = response_message.experiment_id
    username = _get_username_from_auth(authenticate_request())
    store.create_experiment_permission(experiment_id, username, MANAGE.name)


def set_can_manage_registered_model_permission(resp: Response):
    response_message = CreateRegisteredModel.Response()
    parse_dict(resp.json, response_message)
    name = response_message.registered_model.name
    username = _get_username_from_auth(authenticate_request())
    store.create_registered_model_permission(name, username, MANAGE.name)


def delete_can_manage_registered_model_permission(resp: Response):
    """
    Delete registered model permission when the model is deleted.

    We need to do this because the primary key of the registered model is the name,
    unlike the experiment where the primary key is experiment_id (UUID). Therefore,
    we have to delete the permission record when the model is deleted otherwise it
    conflicts with the new model registered with the same name.
    """
    # Get model name from request context because it's not available in the response
    name = request.get_json(force=True, silent=True)["name"]
    username = _get_username_from_auth(authenticate_request())
    store.delete_registered_model_permission(name, username)


def filter_search_experiments(resp: Response):
    if sender_is_admin():
        return

    response_message = SearchExperiments.Response()
    parse_dict(resp.json, response_message)

    # fetch permissions
    username = _get_username_from_auth(authenticate_request())
    perms = store.list_experiment_permissions(username)
    can_read = {p.experiment_id: get_permission(p.permission).can_read for p in perms}
    default_can_read = get_permission(auth_config.default_permission).can_read

    # filter out unreadable
    for e in list(response_message.experiments):
        if not can_read.get(e.experiment_id, default_can_read):
            response_message.experiments.remove(e)

    # re-fetch to fill max results
    request_message = _get_request_message(SearchExperiments())
    while (
        len(response_message.experiments) < request_message.max_results
        and response_message.next_page_token != ""
    ):
        refetched: PagedList[Experiment] = _get_tracking_store().search_experiments(
            view_type=request_message.view_type,
            max_results=request_message.max_results,
            order_by=request_message.order_by,
            filter_string=request_message.filter,
            page_token=response_message.next_page_token,
        )
        refetched = refetched[: request_message.max_results - len(response_message.experiments)]
        if len(refetched) == 0:
            response_message.next_page_token = ""
            break

        refetched_readable_proto = [
            e.to_proto() for e in refetched if can_read.get(e.experiment_id, default_can_read)
        ]
        response_message.experiments.extend(refetched_readable_proto)

        # recalculate next page token
        start_offset = SearchUtils.parse_start_offset_from_page_token(
            response_message.next_page_token
        )
        final_offset = start_offset + len(refetched)
        response_message.next_page_token = SearchUtils.create_page_token(final_offset)

    resp.data = message_to_json(response_message)


def filter_search_logged_models(resp: Response) -> None:
    """
    Filter out unreadable logged models from the search results.
    """
    from mlflow.utils.search_utils import SearchLoggedModelsPaginationToken as Token

    if sender_is_admin():
        return

    response_proto = SearchLoggedModels.Response()
    parse_dict(resp.json, response_proto)

    # fetch permissions
    username = _get_username_from_auth(authenticate_request())
    perms = store.list_experiment_permissions(username)
    can_read = {p.experiment_id: get_permission(p.permission).can_read for p in perms}
    default_can_read = get_permission(auth_config.default_permission).can_read

    # Remove unreadable models
    for m in list(response_proto.models):
        if not can_read.get(m.info.experiment_id, default_can_read):
            response_proto.models.remove(m)

    request_proto = _get_request_message(SearchLoggedModels())
    max_results = request_proto.max_results
    # These parameters won't change in the loop
    params = {
        "experiment_ids": list(request_proto.experiment_ids),
        "filter_string": request_proto.filter or None,
        "order_by": (
            [
                {
                    "field_name": ob.field_name,
                    "ascending": ob.ascending,
                    "dataset_name": ob.dataset_name,
                    "dataset_digest": ob.dataset_digest,
                }
                for ob in request_proto.order_by
            ]
            if request_proto.order_by
            else None
        ),
    }
    next_page_token = response_proto.next_page_token or None
    tracking_store = _get_tracking_store()
    while len(response_proto.models) < max_results and next_page_token is not None:
        batch: PagedList[LoggedModel] = tracking_store.search_logged_models(
            max_results=max_results, page_token=next_page_token, **params
        )
        is_last_page = batch.token is None
        offset = Token.decode(next_page_token).offset if next_page_token else 0
        last_index = len(batch) - 1
        for index, model in enumerate(batch):
            if not can_read.get(model.experiment_id, default_can_read):
                continue
            response_proto.models.append(model.to_proto())
            if len(response_proto.models) >= max_results:
                next_page_token = (
                    None
                    if is_last_page and index == last_index
                    else Token(offset=offset + index + 1, **params).encode()
                )
                break
        else:
            # If we reach here, it means we have not reached the max results.
            next_page_token = (
                None if is_last_page else Token(offset=offset + max_results, **params).encode()
            )

    if next_page_token:
        response_proto.next_page_token = next_page_token
    resp.data = message_to_json(response_proto)


def filter_search_registered_models(resp: Response):
    if sender_is_admin():
        return

    response_message = SearchRegisteredModels.Response()
    parse_dict(resp.json, response_message)

    # fetch permissions
    username = _get_username_from_auth(authenticate_request())
    perms = store.list_registered_model_permissions(username)
    can_read = {p.name: get_permission(p.permission).can_read for p in perms}
    default_can_read = get_permission(auth_config.default_permission).can_read

    # filter out unreadable
    for rm in list(response_message.registered_models):
        if not can_read.get(rm.name, default_can_read):
            response_message.registered_models.remove(rm)

    # re-fetch to fill max results
    request_message = _get_request_message(SearchRegisteredModels())
    while (
        len(response_message.registered_models) < request_message.max_results
        and response_message.next_page_token != ""
    ):
        refetched: PagedList[RegisteredModel] = (
            _get_model_registry_store().search_registered_models(
                filter_string=request_message.filter,
                max_results=request_message.max_results,
                order_by=request_message.order_by,
                page_token=response_message.next_page_token,
            )
        )
        refetched = refetched[
            : request_message.max_results - len(response_message.registered_models)
        ]
        if len(refetched) == 0:
            response_message.next_page_token = ""
            break

        refetched_readable_proto = [
            rm.to_proto() for rm in refetched if can_read.get(rm.name, default_can_read)
        ]
        response_message.registered_models.extend(refetched_readable_proto)

        # recalculate next page token
        start_offset = SearchUtils.parse_start_offset_from_page_token(
            response_message.next_page_token
        )
        final_offset = start_offset + len(refetched)
        response_message.next_page_token = SearchUtils.create_page_token(final_offset)

    resp.data = message_to_json(response_message)


def rename_registered_model_permission(resp: Response):
    """
    A model registry can be assigned to multiple users with different permissions.

    Changing the model registry name must be propagated to all users.
    """
    # get registry model name before update
    data = request.get_json(force=True, silent=True)
    store.rename_registered_model_permissions(data.get("name"), data.get("new_name"))


AFTER_REQUEST_PATH_HANDLERS = {
    CreateExperiment: set_can_manage_experiment_permission,
    CreateRegisteredModel: set_can_manage_registered_model_permission,
    DeleteRegisteredModel: delete_can_manage_registered_model_permission,
    SearchExperiments: filter_search_experiments,
    SearchLoggedModels: filter_search_logged_models,
    SearchRegisteredModels: filter_search_registered_models,
    RenameRegisteredModel: rename_registered_model_permission,
}


def get_after_request_handler(request_class):
    return AFTER_REQUEST_PATH_HANDLERS.get(request_class)


AFTER_REQUEST_HANDLERS = {
    (http_path, method): handler
    for http_path, handler, methods in get_endpoints(get_after_request_handler)
    for method in methods
    if handler is not None and "/graphql" not in http_path
}


@catch_mlflow_exception
def _after_request(resp: Response):
    if 400 <= resp.status_code < 600:
        return resp

    if handler := AFTER_REQUEST_HANDLERS.get((request.path, request.method)):
        handler(resp)
    return resp


def create_admin_user(username, password):
    if not store.has_user(username):
        try:
            store.create_user(username, password, is_admin=True)
            _logger.info(
                f"Created admin user '{username}'. "
                "It is recommended that you set a new password as soon as possible "
                f"on {UPDATE_USER_PASSWORD}."
            )
        except MlflowException as e:
            if isinstance(e.__cause__, sqlalchemy.exc.IntegrityError):
                # When multiple workers are starting up at the same time, it's possible
                # that they try to create the admin user at the same time and one of them
                # will succeed while the others will fail with an IntegrityError.
                return
            raise


def alert(href: str):
    return render_template_string(
        r"""
<script type = "text/javascript">
{% with messages = get_flashed_messages() %}
  {% if messages %}
    {% for message in messages %}
      alert("{{ message }}");
    {% endfor %}
  {% endif %}
{% endwith %}
      window.location.href = "{{ href }}";
</script>
""",
        href=href,
    )


def login():
    """Display login page"""
    return render_template_string(
        r"""
<!DOCTYPE html>
<html>
<head>
    <title>MLflow Login</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            background-color: #f5f5f5;
            margin: 0;
            padding: 0;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
        }
        
        .login-container {
            background-color: white;
            padding: 40px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            max-width: 400px;
            width: 100%;
        }
        
        .logo-container {
            text-align: center;
            margin-bottom: 30px;
        }
        
        .form-group {
            margin-bottom: 20px;
        }
        
        label {
            display: block;
            margin-bottom: 5px;
            font-weight: bold;
            color: #333;
        }
        
        input[type="text"], input[type="password"] {
            width: 100%;
            padding: 12px;
            border: 1px solid #ddd;
            border-radius: 4px;
            font-size: 16px;
            box-sizing: border-box;
        }
        
        input[type="text"]:focus, input[type="password"]:focus {
            outline: none;
            border-color: #2272b4;
        }
        
        .login-button {
            width: 100%;
            padding: 12px;
            background-color: #2272b4;
            color: white;
            border: none;
            border-radius: 4px;
            font-size: 16px;
            font-weight: bold;
            cursor: pointer;
            transition: background-color 0.3s;
        }
        
        .login-button:hover {
            background-color: #0e538b;
        }
        
        .error-message {
            color: #d32f2f;
            margin-top: 10px;
            text-align: center;
        }
        
        .admin-contact-info {
            text-align: center;
            margin-top: 20px;
        }
        
        .admin-contact-info p {
            color: #666;
            font-size: 14px;
            margin: 0;
            padding: 12px;
            background-color: #f8f9fa;
            border-radius: 4px;
            border-left: 4px solid #2272b4;
        }
    </style>
</head>
<body>
    <div class="login-container">
        <div class="logo-container">
            {% autoescape false %}
            {{ mlflow_logo }}
            {% endautoescape %}
            <h2>Login to MLflow</h2>
        </div>
        
        <form method="post">
            <input type="hidden" name="csrf_token" value="{{ csrf_token() }}"/>
            
            <div class="form-group">
                <label for="username">Username:</label>
                <input type="text" id="username" name="username" required>
            </div>
            
            <div class="form-group">
                <label for="password">Password:</label>
                <input type="password" id="password" name="password" required>
            </div>
            
            <button type="submit" class="login-button">Login</button>
        </form>
        
        <div class="admin-contact-info">
            <p>没有账号？请联系管理员开通账号</p>
        </div>
    </div>
    
    {% with messages = get_flashed_messages() %}
        {% if messages %}
            <script>
                {% for message in messages %}
                    alert("{{ message }}");
                {% endfor %}
            </script>
        {% endif %}
    {% endwith %}
</body>
</html>
""",
        mlflow_logo=MLFLOW_LOGO,
    )


@catch_mlflow_exception
def handle_login(csrf):
    """Handle login form submission"""
    csrf.protect()
    
    if request.method == "GET":
        return login()
    
    username = request.form.get("username")
    password = request.form.get("password")
    
    if not username or not password:
        flash("Username and password cannot be empty")
        return login()
    
    # Verify user credentials
    if store.authenticate_user(username, password):
        # Login successful, set session
        user = store.get_user(username)
        session.permanent = True
        session["username"] = username
        session["user_id"] = user.id
        session["is_admin"] = user.is_admin
        session["login_time"] = datetime.now().isoformat()
        
        # Redirect to original page or home
        next_page = request.args.get("next")
        if next_page:
            return redirect(next_page)
        else:
            return redirect(HOME)
    else:
        flash("Invalid username or password")
        return login()


def handle_logout():
    """Handle user logout"""
    session.clear()
    flash("You have been logged out successfully")
    return redirect(LOGIN)


def admin_panel_guard(func):
    """Admin panel permission guard decorator"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Check if user is logged in
        if "username" not in session:
            return redirect(LOGIN)
        
        # Check if user is admin
        if not session.get("is_admin", False):
            return make_response("Access denied: Admin privileges required", 403)
        
        return func(*args, **kwargs)
    return wrapper


@admin_panel_guard
def admin_dashboard():
    """Admin panel dashboard"""
    # Get statistics
    users = store.list_users()
    user_count = len(users)
    admin_count = len([u for u in users if u.is_admin])
    
    # Get total number of experiments
    try:
        experiments = _get_tracking_store().search_experiments()
        experiment_count = len(experiments)
    except Exception:
        experiment_count = 0
    
    return render_template_string(
        r"""
<!DOCTYPE html>
<html>
<head>
    <title>MLflow 管理面板</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 0;
            background-color: #f5f5f5;
        }
        
        .header {
            background-color: #2272b4;
            color: white;
            padding: 1rem 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .header h1 {
            margin: 0;
        }
        
        .logout-btn {
            background-color: #0e538b;
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 4px;
            cursor: pointer;
            text-decoration: none;
        }
        
        .logout-btn:hover {
            background-color: #0a4470;
        }
        
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 2rem;
        }
        
        .welcome {
            background-color: white;
            padding: 2rem;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 2rem;
        }
        
        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 1rem;
            margin-bottom: 2rem;
        }
        
        .stat-card {
            background-color: white;
            padding: 1.5rem;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            text-align: center;
        }
        
        .stat-number {
            font-size: 2rem;
            font-weight: bold;
            color: #2272b4;
        }
        
        .stat-label {
            color: #666;
            margin-top: 0.5rem;
        }
        
        .actions {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 1rem;
        }
        
        .action-card {
            background-color: white;
            padding: 2rem;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            text-align: center;
        }
        
        .action-btn {
            display: inline-block;
            background-color: #2272b4;
            color: white;
            padding: 12px 24px;
            border-radius: 4px;
            text-decoration: none;
            margin-top: 1rem;
            transition: background-color 0.3s;
        }
        
        .action-btn:hover {
            background-color: #0e538b;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>MLflow 管理面板</h1>
        <div>
            <span>欢迎，{{ current_user }}（管理员）</span>
            <a href="{{ home_url }}" class="logout-btn">主界面</a>
            <a href="{{ logout_url }}" class="logout-btn">登出</a>
        </div>
    </div>
    
    <div class="container">
        <div class="welcome">
            <h2>系统概览</h2>
            <p>欢迎来到 MLflow 管理面板。您可以在这里管理用户、权限和系统设置。</p>
        </div>
        
        <div class="stats">
            <div class="stat-card">
                <div class="stat-number">{{ user_count }}</div>
                <div class="stat-label">总用户数</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{{ admin_count }}</div>
                <div class="stat-label">管理员数</div>
            </div>
            <div class="stat-card">
                <div class="stat-number">{{ experiment_count }}</div>
                <div class="stat-label">实验总数</div>
            </div>
        </div>
        
        <div class="actions">
            <div class="action-card">
                <h3>用户管理</h3>
                <p>查看、创建和管理系统用户，设置管理员权限。</p>
                <a href="{{ users_url }}" class="action-btn">管理用户</a>
            </div>
            <div class="action-card">
                <h3>实验管理</h3>
                <p>管理用户对实验的访问权限，包括读取、编辑和管理权限。</p>
                <a href="{{ experiments_url }}" class="action-btn">管理实验</a>
            </div>
        </div>
    </div>
</body>
</html>
""",
        current_user=session.get("username"),
        home_url=HOME,
        logout_url=LOGOUT,
        users_url=ADMIN_USERS,
        experiments_url=ADMIN_EXPERIMENTS,
        user_count=user_count,
        admin_count=admin_count,
        experiment_count=experiment_count,
    )


@admin_panel_guard
def admin_users_page():
    """Admin users management page"""
    users = store.list_users()
    
    return render_template_string(
        r"""
<!DOCTYPE html>
<html>
<head>
    <title>用户管理 - MLflow 管理面板</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 0;
            background-color: #f5f5f5;
        }
        
        .header {
            background-color: #2272b4;
            color: white;
            padding: 1rem 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .header h1 {
            margin: 0;
        }
        
        .nav-links a {
            color: white;
            text-decoration: none;
            margin-left: 1rem;
            padding: 8px 16px;
            border-radius: 4px;
            background-color: #0e538b;
        }
        
        .nav-links a:hover {
            background-color: #0a4470;
        }
        
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 2rem;
        }
        
        .section {
            background-color: white;
            padding: 2rem;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 2rem;
        }
        
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 1rem;
        }
        
        th, td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }
        
        th {
            background-color: #f8f9fa;
            font-weight: bold;
        }
        
        .admin-badge {
            background-color: #28a745;
            color: white;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 12px;
        }
        
        .form-group {
            margin-bottom: 1rem;
        }
        
        label {
            display: block;
            margin-bottom: 0.5rem;
            font-weight: bold;
        }
        
        input[type="text"], input[type="password"] {
            width: 100%;
            padding: 8px;
            border: 1px solid #ddd;
            border-radius: 4px;
            box-sizing: border-box;
        }
        
        .checkbox-group {
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }
        
        .btn {
            background-color: #2272b4;
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
        }
        
        .btn:hover {
            background-color: #0e538b;
        }
        
        .btn-success {
            background-color: #28a745;
        }
        
        .btn-success:hover {
            background-color: #218838;
        }
        
        .btn-danger {
            background-color: #dc3545;
            color: white;
            border: none;
            padding: 6px 12px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 12px;
        }
        
        .btn-danger:hover {
            background-color: #c82333;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>用户管理</h1>
        <div class="nav-links">
            <a href="{{ home_url }}">主界面</a>
            <a href="{{ admin_url }}">管理面板</a>
            <a href="{{ logout_url }}">登出</a>
        </div>
    </div>
    
    <div class="container">
        <!-- 用户列表 -->
        <div class="section">
            <h2>系统用户</h2>
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>用户名</th>
                        <th>角色</th>
                        <th>创建时间</th>
                        <th>操作</th>
                    </tr>
                </thead>
                <tbody>
                    {% for user in users %}
                    <tr>
                        <td>{{ user.id }}</td>
                        <td>{{ user.username }}</td>
                        <td>
                            {% if user.is_admin %}
                                <span class="admin-badge">管理员</span>
                            {% else %}
                                普通用户
                            {% endif %}
                        </td>
                        <td>-</td>
                        <td>
                            {% if user.username == current_user %}
                            <span style="color: #6c757d; font-style: italic;">当前用户</span>
                            {% elif current_user == "admin" or (current_user != "admin" and user.username != "admin" and not user.is_admin) %}
                            <form method="post" action="{{ url_for('admin_delete_user', user_id=user.id) }}" 
                                  style="display: inline-block;" 
                                  data-username="{{ user.username }}"
                                  onsubmit="return confirmDeleteUser(this.dataset.username)">
                                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}"/>
                                <button type="submit" class="btn btn-danger">删除</button>
                            </form>
                            {% else %}
                            <span style="color: #6c757d; font-style: italic;">无权限删除</span>
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        
        <!-- 创建用户表单 -->
        <div class="section">
            <h2>创建新用户</h2>
            <form method="post">
                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}"/>
                
                <div class="form-group">
                    <label for="username">用户名:</label>
                    <input type="text" id="username" name="username" required minlength="4">
                </div>
                
                <div class="form-group">
                    <label for="password">密码:</label>
                    <input type="password" id="password" name="password" required minlength="8">
                </div>
                
                <div class="form-group">
                    <div class="checkbox-group">
                        <input type="checkbox" id="is_admin" name="is_admin" value="true">
                        <label for="is_admin">设为管理员</label>
                    </div>
                </div>
                
                <button type="submit" class="btn btn-success">创建用户</button>
            </form>
        </div>
    </div>
    
    {% with messages = get_flashed_messages() %}
        {% if messages %}
            <script>
                {% for message in messages %}
                    alert("{{ message }}");
                {% endfor %}
            </script>
        {% endif %}
    {% endwith %}
    
    <script>
        function confirmDeleteUser(username) {
            console.log('confirmDeleteUser called with username:', username);
            var message = '确定要删除用户 "' + username + '" 吗？此操作不可撤销！';
            console.log('Confirm message:', message);
            var result = confirm(message);
            console.log('User confirmation result:', result);
            return result;
        }
        
        // 页面加载时检查JavaScript
        document.addEventListener('DOMContentLoaded', function() {
            console.log('User management page loaded');
            var forms = document.querySelectorAll('form[onsubmit*="confirmDeleteUser"]');
            console.log('Found delete user forms:', forms.length);
        });
    </script>
</body>
</html>
""",
        users=users,
        current_user=session.get("username"),
        home_url=HOME,
        admin_url=ADMIN,
        logout_url=LOGOUT,
    )


@admin_panel_guard
@catch_mlflow_exception
def admin_create_user(csrf):
    """Handle admin user creation"""
    csrf.protect()
    
    if request.method == "GET":
        return admin_users_page()
    
    username = request.form.get("username")
    password = request.form.get("password")
    is_admin = request.form.get("is_admin") == "true"
    
    if not username or not password:
        flash("Username and password cannot be empty")
        return admin_users_page()
    
    try:
        store.create_user(username, password, is_admin=is_admin)
        role = "admin" if is_admin else "regular"
        flash(f"Successfully created {role} user: {username}")
    except MlflowException as e:
        flash(f"Failed to create user: {str(e)}")
    
    return admin_users_page()


@admin_panel_guard
@catch_mlflow_exception
def admin_delete_user(csrf, user_id):
    """Handle admin user deletion"""
    _logger.info(f"admin_delete_user called, method: {request.method}, user_id: {user_id}")
    _logger.info(f"Request form data: {dict(request.form)}")
    _logger.info(f"Request args: {dict(request.args)}")
    
    csrf.protect()
    
    if request.method == "GET":
        _logger.info("GET request, redirecting to admin_users_page")
        return admin_users_page()
    
    current_user = session.get("username")
    
    _logger.info(f"Attempting to delete user_id: {user_id}, current user: {current_user}")
    
    if not user_id:
        _logger.warning("User ID is empty")
        flash("用户ID不能为空")
        return admin_users_page()
    
    try:
        # Check if user exists
        _logger.info(f"Checking if user exists with ID: {user_id}")
        target_user = store.get_user_by_id(int(user_id))
        current_user_obj = store.get_user(current_user)
        _logger.info(f"Target user found: {target_user.username}, is_admin: {target_user.is_admin}")
        _logger.info(f"Current user: {current_user_obj.username}, is_admin: {current_user_obj.is_admin}")
        
        # Check if trying to delete self
        if target_user.username == current_user:
            _logger.warning(f"Cannot delete current user: {target_user.username}")
            flash("无法删除当前登录用户")
            return admin_users_page()
        
        # Enhanced permission check
        if current_user != "admin" and not current_user_obj.is_admin:  # Non-admin users
            _logger.info("Current user is not admin, checking permissions")
            if target_user.username == "admin":
                _logger.warning("Non-admin trying to delete admin account")
                flash("非admin管理员不能删除admin账号")
                return admin_users_page()
            
            if target_user.is_admin:
                _logger.warning("Non-admin trying to delete another admin account")
                flash("非admin管理员不能删除其他管理员账号")
                return admin_users_page()
        elif current_user != "admin" and current_user_obj.is_admin:  # Non-admin administrators
            _logger.info("Current user is admin but not 'admin' user, checking permissions")
            if target_user.username == "admin":
                _logger.warning("Non-admin administrator trying to delete admin account")
                flash("非admin管理员不能删除admin账号")
                return admin_users_page()
            
            if target_user.is_admin and target_user.username != current_user:
                _logger.warning("Non-admin administrator trying to delete another admin account")
                flash("非admin管理员不能删除其他管理员账号")
                return admin_users_page()
        else:
            _logger.info("Current user is admin, all permissions granted")
        
        # Delete user and related permissions manually (in case CASCADE is not set up)
        _logger.info(f"Permission check passed, deleting user: {target_user.username} (ID: {user_id})")
        
        # First, delete all experiment permissions for this user
        try:
            experiment_permissions = store.list_experiment_permissions(target_user.username)
            for perm in experiment_permissions:
                store.delete_experiment_permission(perm.experiment_id, target_user.username)
            _logger.info(f"Deleted {len(experiment_permissions)} experiment permissions for user {target_user.username}")
        except Exception as e:
            _logger.warning(f"Error deleting experiment permissions for user {target_user.username}: {e}")
        
        # Delete all registered model permissions for this user
        try:
            model_permissions = store.list_registered_model_permissions(target_user.username)
            for perm in model_permissions:
                store.delete_registered_model_permission(perm.name, target_user.username)
            _logger.info(f"Deleted {len(model_permissions)} model permissions for user {target_user.username}")
        except Exception as e:
            _logger.warning(f"Error deleting model permissions for user {target_user.username}: {e}")
        
        # Finally, delete the user
        store.delete_user_by_id(int(user_id))
        _logger.info(f"User deleted successfully: {target_user.username}")
        
        flash(f"成功删除用户: {target_user.username}")
    except MlflowException as e:
        _logger.error(f"MlflowException when deleting user_id {user_id}: {str(e)}")
        flash(f"删除用户失败: {str(e)}")
    except Exception as e:
        _logger.error(f"Exception when deleting user_id {user_id}: {str(e)}")
        flash(f"删除用户时发生错误: {str(e)}")
    
    return admin_users_page()


@admin_panel_guard
def admin_experiments_page():
    """Experiment permission management page - experiment list with create/delete functionality"""
    try:
        # Get all experiments
        experiments = _get_tracking_store().search_experiments()
    except Exception as e:
        experiments = []
        flash(f"Failed to get experiment list: {str(e)}")
    
    return render_template_string(
        r"""
<!DOCTYPE html>
<html>
<head>
    <title>权限管理 - MLflow 管理面板</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 0;
            background-color: #f5f5f5;
        }
        
        .header {
            background-color: #2272b4;
            color: white;
            padding: 1rem 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .header h1 {
            margin: 0;
        }
        
        .nav-links a {
            color: white;
            text-decoration: none;
            margin-left: 1rem;
            padding: 8px 16px;
            border-radius: 4px;
            background-color: #0e538b;
        }
        
        .nav-links a:hover {
            background-color: #0a4470;
        }
        
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 2rem;
        }
        
        .section {
            background-color: white;
            padding: 2rem;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 2rem;
        }
        
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 1rem;
        }
        
        th, td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }
        
        th {
            background-color: #f8f9fa;
            font-weight: bold;
        }
        
        .btn {
            background-color: #2272b4;
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 4px;
            cursor: pointer;
            text-decoration: none;
            font-size: 14px;
            display: inline-block;
            margin-right: 5px;
        }
        
        .btn:hover {
            background-color: #0e538b;
        }
        
        .btn-danger {
            background-color: #dc3545;
        }
        
        .btn-danger:hover {
            background-color: #c82333;
        }
        
        .btn-success {
            background-color: #28a745;
        }
        
        .btn-success:hover {
            background-color: #218838;
        }
        
        .form-group {
            margin-bottom: 1rem;
        }
        
        label {
            display: block;
            margin-bottom: 0.5rem;
            font-weight: bold;
        }
        
        input[type="text"] {
            width: 100%;
            padding: 8px;
            border: 1px solid #ddd;
            border-radius: 4px;
            box-sizing: border-box;
        }
        
        .action-buttons {
            display: flex;
            gap: 5px;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>实验权限管理</h1>
        <div class="nav-links">
            <a href="{{ home_url }}">主界面</a>
            <a href="{{ admin_url }}">管理面板</a>
            <a href="{{ logout_url }}">登出</a>
        </div>
    </div>
    
    <div class="container">
        <!-- 实验列表 -->
        <div class="section">
            <h2>实验列表</h2>
            <p>管理实验和用户权限：</p>
            
            {% if experiments %}
            <table>
                <thead>
                    <tr>
                        <th>实验ID</th>
                        <th>实验名称</th>
                        <th>生命周期状态</th>
                        <th>操作</th>
                    </tr>
                </thead>
                <tbody>
                    {% for exp in experiments %}
                    <tr>
                        <td>{{ exp.experiment_id }}</td>
                        <td>{{ exp.name }}</td>
                        <td>{{ exp.lifecycle_stage }}</td>
                        <td>
                            <div class="action-buttons">
                                <a href="{{ url_for('admin_experiment_permission', experiment_id=exp.experiment_id) }}" class="btn">管理权限</a>
                                {% if exp.name != "Default" %}
                                <form method="post" action="{{ url_for('admin_delete_experiment', experiment_id=exp.experiment_id) }}" 
                                      style="display: inline-block;" 
                                      onsubmit="return confirmDeleteExperiment('{{ exp.name|e }}')">
                                    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}"/>
                                    <button type="submit" class="btn btn-danger">删除</button>
                                </form>
                                {% endif %}
                            </div>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
            <p>暂无实验数据。</p>
            {% endif %}
        </div>
        
        <!-- 新建实验表单 -->
        <div class="section">
            <h2>创建新实验</h2>
            <form method="post" action="{{ create_experiment_url }}">
                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}"/>
                
                <div class="form-group">
                    <label for="experiment_name">实验名称:</label>
                    <input type="text" id="experiment_name" name="experiment_name" required 
                           placeholder="输入实验名称" minlength="1">
                </div>
                
                <button type="submit" class="btn btn-success">创建实验</button>
            </form>
        </div>
    </div>
    
    {% with messages = get_flashed_messages() %}
        {% if messages %}
            <script>
                {% for message in messages %}
                    alert("{{ message }}");
                {% endfor %}
            </script>
        {% endif %}
    {% endwith %}
    
    <script>
        function confirmDeleteExperiment(experimentName) {
            return confirm('确定要删除实验 "' + experimentName + '" 吗？此操作不可撤销！');
        }
    </script>
</body>
</html>
""",
        experiments=experiments,
        home_url=HOME,
        admin_url=ADMIN,
        logout_url=LOGOUT,
        create_experiment_url=ADMIN_CREATE_EXPERIMENT,
    )


@admin_panel_guard
def admin_manage_experiment_permission_page(experiment_id):
    """Single experiment permission configuration page"""
    try:
        # Get experiment info
        experiment = _get_tracking_store().get_experiment(experiment_id)
        
        # Get all users
        all_users = store.list_users()
        
        # Get all permission assignments for this experiment
        permissions = store.get_all_permissions_for_experiment(experiment_id)
        permission_dict = {p["username"]: p["permission"] for p in permissions}
        
        # Get all available permission levels
        from mlflow.server.auth.permissions import ALL_PERMISSIONS
        available_permissions = list(ALL_PERMISSIONS.keys())
        
    except Exception as e:
        flash(f"Failed to get experiment info: {str(e)}")
        return redirect(ADMIN_EXPERIMENTS)
    
    return render_template_string(
        r"""
<!DOCTYPE html>
<html>
<head>
    <title>实验权限管理 - {{ experiment.name }}</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 0;
            background-color: #f5f5f5;
        }
        
        .header {
            background-color: #2272b4;
            color: white;
            padding: 1rem 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .header h1 {
            margin: 0;
        }
        
        .nav-links a {
            color: white;
            text-decoration: none;
            margin-left: 1rem;
            padding: 8px 16px;
            border-radius: 4px;
            background-color: #0e538b;
        }
        
        .nav-links a:hover {
            background-color: #0a4470;
        }
        
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 2rem;
        }
        
        .section {
            background-color: white;
            padding: 2rem;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 2rem;
        }
        
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 1rem;
        }
        
        th, td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #ddd;
            vertical-align: middle;
        }
        
        th {
            background-color: #f8f9fa;
            font-weight: bold;
        }
        
        select {
            padding: 6px 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            margin-right: 10px;
        }
        
        .btn {
            background-color: #2272b4;
            color: white;
            border: none;
            padding: 6px 12px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
        }
        
        .btn:hover {
            background-color: #0e538b;
        }
        
        .current-permission {
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 12px;
            color: white;
        }
        
        .permission-READ {
            background-color: #17a2b8;
        }
        
        .permission-EDIT {
            background-color: #ffc107;
            color: #212529;
        }
        
        .permission-MANAGE {
            background-color: #28a745;
        }
        
        .permission-NO_PERMISSIONS {
            background-color: #6c757d;
        }
        
        .no-permission {
            color: #6c757d;
            font-style: italic;
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>{{ experiment.name }} - 权限管理</h1>
        <div class="nav-links">
            <a href="{{ home_url }}">主界面</a>
            <a href="{{ admin_url }}">管理面板</a>
            <a href="{{ experiments_url }}">实验列表</a>
            <a href="{{ logout_url }}">登出</a>
        </div>
    </div>
    
    <div class="container">
        <div class="section">
            <h2>实验信息</h2>
            <p><strong>实验ID:</strong> {{ experiment.experiment_id }}</p>
            <p><strong>实验名称:</strong> {{ experiment.name }}</p>
            <p><strong>生命周期状态:</strong> {{ experiment.lifecycle_stage }}</p>
        </div>
        
        <div class="section">
            <h2>用户权限管理</h2>
            <table>
                <thead>
                    <tr>
                        <th>用户名</th>
                        <th>当前权限</th>
                        <th>设置新权限</th>
                    </tr>
                </thead>
                <tbody>
                    {% for user in all_users %}
                    <tr>
                        <td>{{ user.username }}</td>
                        <td>
                            {% if user.username in permission_dict %}
                                <span class="current-permission permission-{{ permission_dict[user.username] }}">
                                    {{ permission_dict[user.username] }}
                                </span>
                            {% else %}
                                <span class="no-permission">无权限</span>
                            {% endif %}
                        </td>
                        <td>
                            <form method="post" style="display: inline-block;">
                                <input type="hidden" name="csrf_token" value="{{ csrf_token() }}"/>
                                <input type="hidden" name="username" value="{{ user.username }}"/>
                                
                                <select name="permission">
                                    {% for perm in available_permissions %}
                                    <option value="{{ perm }}" 
                                        {% if permission_dict.get(user.username) == perm %}selected{% endif %}>
                                        {{ perm }}
                                    </option>
                                    {% endfor %}
                                </select>
                                
                                <button type="submit" class="btn">更新</button>
                            </form>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
    
    {% with messages = get_flashed_messages() %}
        {% if messages %}
            <script>
                {% for message in messages %}
                    alert("{{ message }}");
                {% endfor %}
            </script>
        {% endif %}
    {% endwith %}
</body>
</html>
""",
        experiment=experiment,
        all_users=all_users,
        permission_dict=permission_dict,
        available_permissions=available_permissions,
        home_url=HOME,
        admin_url=ADMIN,
        experiments_url=ADMIN_EXPERIMENTS,
        logout_url=LOGOUT,
    )


@admin_panel_guard
@catch_mlflow_exception
def admin_update_experiment_permission(csrf, experiment_id):
    """Update experiment permission"""
    csrf.protect()
    
    if request.method == "GET":
        return admin_manage_experiment_permission_page(experiment_id)
    
    username = request.form.get("username")
    permission = request.form.get("permission")
    
    if not username or not permission:
        flash("Username and permission cannot be empty")
        return admin_manage_experiment_permission_page(experiment_id)
    
    try:
        store.update_or_create_experiment_permission(experiment_id, username, permission)
        flash(f"Successfully updated permission for user {username} to {permission}")
    except MlflowException as e:
        flash(f"Failed to update permission: {str(e)}")
    
    return admin_manage_experiment_permission_page(experiment_id)


@admin_panel_guard
@catch_mlflow_exception
def admin_create_experiment(csrf):
    """Handle admin experiment creation"""
    csrf.protect()
    
    if request.method == "GET":
        return admin_experiments_page()
    
    experiment_name = request.form.get("experiment_name")
    
    if not experiment_name or not experiment_name.strip():
        flash("实验名称不能为空")
        return admin_experiments_page()
    
    try:
        # Create experiment using tracking store
        tracking_store = _get_tracking_store()
        experiment_id = tracking_store.create_experiment(experiment_name.strip())
        
        # Automatically grant MANAGE permission to the creator
        username = session.get("username")
        if username:
            store.create_experiment_permission(experiment_id, username, MANAGE.name)
        
        flash(f"成功创建实验: {experiment_name}")
    except MlflowException as e:
        flash(f"创建实验失败: {str(e)}")
    except Exception as e:
        flash(f"创建实验时发生错误: {str(e)}")
    
    return admin_experiments_page()


@admin_panel_guard
@catch_mlflow_exception
def admin_delete_experiment(csrf, experiment_id):
    """Handle admin experiment deletion"""
    csrf.protect()
    
    if request.method == "GET":
        return admin_experiments_page()
    
    try:
        # Get experiment info first for logging
        tracking_store = _get_tracking_store()
        experiment = tracking_store.get_experiment(experiment_id)
        
        if experiment.name == "Default":
            flash("无法删除默认实验")
            return admin_experiments_page()
        
        # Delete experiment using tracking store
        tracking_store.delete_experiment(experiment_id)
        
        # Clean up permissions (they should be cleaned up automatically by foreign key constraints)
        # But we can also clean them up explicitly to be safe
        try:
            # Get all permissions for this experiment and delete them
            permissions = store.get_all_permissions_for_experiment(experiment_id)
            for perm in permissions:
                try:
                    store.delete_experiment_permission(experiment_id, perm["username"])
                except Exception:
                    pass  # Ignore cleanup errors
        except Exception:
            pass  # Ignore cleanup errors
        
        flash(f"成功删除实验: {experiment.name}")
    except MlflowException as e:
        flash(f"删除实验失败: {str(e)}")
    except Exception as e:
        flash(f"删除实验时发生错误: {str(e)}")
    
    return admin_experiments_page()


def signup():
    return render_template_string(
        r"""
<style>
  form {
    background-color: #F5F5F5;
    border: 1px solid #CCCCCC;
    border-radius: 4px;
    padding: 20px;
    max-width: 400px;
    margin: 0 auto;
    font-family: Arial, sans-serif;
    font-size: 14px;
    line-height: 1.5;
  }

  input[type=text], input[type=password] {
    width: 100%;
    padding: 10px;
    margin-bottom: 10px;
    border: 1px solid #CCCCCC;
    border-radius: 4px;
    box-sizing: border-box;
  }
  input[type=submit] {
    background-color: rgb(34, 114, 180);
    color: #FFFFFF;
    border: none;
    border-radius: 4px;
    padding: 10px 20px;
    cursor: pointer;
    font-size: 16px;
    font-weight: bold;
  }

  input[type=submit]:hover {
    background-color: rgb(14, 83, 139);
  }

  .logo-container {
    display: flex;
    align-items: center;
    justify-content: center;
    margin-bottom: 10px;
  }

  .logo {
    max-width: 150px;
    margin-right: 10px;
  }
</style>

<form action="{{ users_route }}" method="post">
  <input type="hidden" name="csrf_token" value="{{ csrf_token() }}"/>
  <div class="logo-container">
    {% autoescape false %}
    {{ mlflow_logo }}
    {% endautoescape %}
  </div>
  <label for="username">Username:</label>
  <br>
  <input type="text" id="username" name="username" minlength="4">
  <br>
  <label for="password">Password:</label>
  <br>
  <input type="password" id="password" name="password" minlength="12">
  <br>
  <br>
  <input type="submit" value="Sign up">
</form>
""",
        mlflow_logo=MLFLOW_LOGO,
        users_route=CREATE_USER_UI,
    )


@catch_mlflow_exception
def create_user_ui(csrf):
    csrf.protect()
    content_type = request.headers.get("Content-Type")
    if content_type == "application/x-www-form-urlencoded":
        username = request.form["username"]
        password = request.form["password"]

        if not username or not password:
            message = "Username and password cannot be empty."
            return make_response(message, 400)

        if store.has_user(username):
            flash(f"Username has already been taken: {username}")
            return alert(href=SIGNUP)

        store.create_user(username, password)
        flash(f"Successfully signed up user: {username}")
        return alert(href=HOME)
    else:
        message = "Invalid content type. Must be application/x-www-form-urlencoded"
        return make_response(message, 400)


@catch_mlflow_exception
def create_user():
    content_type = request.headers.get("Content-Type")
    if content_type == "application/json":
        username = _get_request_param("username")
        password = _get_request_param("password")

        if not username or not password:
            message = "Username and password cannot be empty."
            return make_response(message, 400)

        user = store.create_user(username, password)
        return jsonify({"user": user.to_json()})
    else:
        message = "Invalid content type. Must be application/json"
        return make_response(message, 400)


@catch_mlflow_exception
def get_user():
    username = _get_request_param("username")
    user = store.get_user(username)
    return jsonify({"user": user.to_json()})


@catch_mlflow_exception
def update_user_password():
    username = _get_request_param("username")
    password = _get_request_param("password")
    store.update_user(username, password=password)
    return make_response({})


@catch_mlflow_exception
def update_user_admin():
    username = _get_request_param("username")
    is_admin = _get_request_param("is_admin")
    store.update_user(username, is_admin=is_admin)
    return make_response({})


@catch_mlflow_exception
def delete_user():
    username = _get_request_param("username")
    store.delete_user(username)
    return make_response({})


@catch_mlflow_exception
def create_experiment_permission():
    experiment_id = _get_request_param("experiment_id")
    username = _get_request_param("username")
    permission = _get_request_param("permission")
    ep = store.create_experiment_permission(experiment_id, username, permission)
    return jsonify({"experiment_permission": ep.to_json()})


@catch_mlflow_exception
def get_experiment_permission():
    experiment_id = _get_request_param("experiment_id")
    username = _get_request_param("username")
    ep = store.get_experiment_permission(experiment_id, username)
    return make_response({"experiment_permission": ep.to_json()})


@catch_mlflow_exception
def update_experiment_permission():
    experiment_id = _get_request_param("experiment_id")
    username = _get_request_param("username")
    permission = _get_request_param("permission")
    store.update_experiment_permission(experiment_id, username, permission)
    return make_response({})


@catch_mlflow_exception
def delete_experiment_permission():
    experiment_id = _get_request_param("experiment_id")
    username = _get_request_param("username")
    store.delete_experiment_permission(experiment_id, username)
    return make_response({})


@catch_mlflow_exception
def create_registered_model_permission():
    name = _get_request_param("name")
    username = _get_request_param("username")
    permission = _get_request_param("permission")
    rmp = store.create_registered_model_permission(name, username, permission)
    return make_response({"registered_model_permission": rmp.to_json()})


@catch_mlflow_exception
def get_registered_model_permission():
    name = _get_request_param("name")
    username = _get_request_param("username")
    rmp = store.get_registered_model_permission(name, username)
    return make_response({"registered_model_permission": rmp.to_json()})


@catch_mlflow_exception
def update_registered_model_permission():
    name = _get_request_param("name")
    username = _get_request_param("username")
    permission = _get_request_param("permission")
    store.update_registered_model_permission(name, username, permission)
    return make_response({})


@catch_mlflow_exception
def delete_registered_model_permission():
    name = _get_request_param("name")
    username = _get_request_param("username")
    store.delete_registered_model_permission(name, username)
    return make_response({})


def create_app(app: Flask = base_app):
    """
    A factory to enable authentication and authorization for the MLflow server.

    Args:
        app: The Flask app to enable authentication and authorization for.

    Returns:
        The app with authentication and authorization enabled.
    """
    _logger.warning(
        "This feature is still experimental and may change in a future release without warning"
    )

    # a secret key is required for flashing, and also for
    # CSRF protection. it's important that this is a static key,
    # otherwise CSRF validation won't work across workers.
    secret_key = MLFLOW_FLASK_SERVER_SECRET_KEY.get()
    if not secret_key:
        raise MlflowException(
            "A static secret key needs to be set for CSRF protection. Please set the "
            "`MLFLOW_FLASK_SERVER_SECRET_KEY` environment variable before starting the "
            "server. For example:\n\n"
            "export MLFLOW_FLASK_SERVER_SECRET_KEY='my-secret-key'\n\n"
            "If you are using multiple servers, please ensure this key is consistent between "
            "them, in order to prevent validation issues."
        )
    app.secret_key = secret_key

    # 初始化数据库和store（必须在配置会话前完成）
    # 使用线程安全的方式初始化，避免多worker竞争
    try:
        store.init_db(auth_config.database_uri)
        create_admin_user(auth_config.admin_username, auth_config.admin_password)
    except Exception as e:
        # 如果初始化失败，可能是因为已经被其他worker初始化了
        _logger.warning(f"Database initialization warning: {e}")
        # 重新连接数据库以确保连接正常
        try:
            store.init_db(auth_config.database_uri)
        except Exception:
            pass  # 忽略重连错误
    
    # 配置会话管理
    session_config = auth_config.session_config
    for key, value in session_config.items():
        app.config[key] = value
    
    # 初始化Flask-Session
    Session(app)

    # we only need to protect the CREATE_USER_UI and login routes
    app.config["WTF_CSRF_CHECK_DEFAULT"] = False
    csrf = CSRFProtect()
    csrf.init_app(app)

    # 创建具名的视图函数以避免lambda冲突
    def login_view():
        return handle_login(csrf)
    
    def create_user_ui_view():
        return create_user_ui(csrf)
    
    def admin_users_view():
        return admin_create_user(csrf)
    
    def admin_experiment_permission_view(experiment_id):
        return admin_update_experiment_permission(csrf, experiment_id)
    
    def admin_create_experiment_view():
        return admin_create_experiment(csrf)
    
    def admin_delete_experiment_view(experiment_id):
        return admin_delete_experiment(csrf, experiment_id)
    
    def admin_delete_user_view(user_id):
        return admin_delete_user(csrf, user_id)
    
    # 添加登录和登出路由
    app.add_url_rule(
        rule=LOGIN,
        endpoint="login",
        view_func=login_view,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        rule=LOGOUT,
        endpoint="logout",
        view_func=handle_logout,
        methods=["GET"],
    )
    app.add_url_rule(
        rule=SIGNUP,
        endpoint="signup",
        view_func=signup,
        methods=["GET"],
    )
    app.add_url_rule(
        rule=CREATE_USER_UI,
        endpoint="create_user_ui",
        view_func=create_user_ui_view,
        methods=["POST"],
    )
    
    # 管理员面板路由
    app.add_url_rule(
        rule=ADMIN,
        endpoint="admin_dashboard",
        view_func=admin_dashboard,
        methods=["GET"],
    )
    app.add_url_rule(
        rule=ADMIN_USERS,
        endpoint="admin_users",
        view_func=admin_users_view,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        rule=ADMIN_DELETE_USER,
        endpoint="admin_delete_user",
        view_func=admin_delete_user_view,
        methods=["POST"],
    )
    app.add_url_rule(
        rule=ADMIN_EXPERIMENTS,
        endpoint="admin_experiments",
        view_func=admin_experiments_page,
        methods=["GET"],
    )
    app.add_url_rule(
        rule=ADMIN_EXPERIMENT_PERMISSION,
        endpoint="admin_experiment_permission",
        view_func=admin_experiment_permission_view,
        methods=["GET", "POST"],
    )
    app.add_url_rule(
        rule=ADMIN_CREATE_EXPERIMENT,
        endpoint="admin_create_experiment",
        view_func=admin_create_experiment_view,
        methods=["POST"],
    )
    app.add_url_rule(
        rule=ADMIN_DELETE_EXPERIMENT,
        endpoint="admin_delete_experiment",
        view_func=admin_delete_experiment_view,
        methods=["POST"],
    )
    app.add_url_rule(
        rule=CREATE_USER,
        endpoint="create_user",
        view_func=create_user,
        methods=["POST"],
    )
    app.add_url_rule(
        rule=GET_USER,
        endpoint="get_user",
        view_func=get_user,
        methods=["GET"],
    )
    app.add_url_rule(
        rule=UPDATE_USER_PASSWORD,
        endpoint="update_user_password",
        view_func=update_user_password,
        methods=["PATCH"],
    )
    app.add_url_rule(
        rule=UPDATE_USER_ADMIN,
        endpoint="update_user_admin",
        view_func=update_user_admin,
        methods=["PATCH"],
    )
    app.add_url_rule(
        rule=DELETE_USER,
        endpoint="delete_user",
        view_func=delete_user,
        methods=["DELETE"],
    )
    app.add_url_rule(
        rule=CREATE_EXPERIMENT_PERMISSION,
        endpoint="create_experiment_permission",
        view_func=create_experiment_permission,
        methods=["POST"],
    )
    app.add_url_rule(
        rule=GET_EXPERIMENT_PERMISSION,
        endpoint="get_experiment_permission",
        view_func=get_experiment_permission,
        methods=["GET"],
    )
    app.add_url_rule(
        rule=UPDATE_EXPERIMENT_PERMISSION,
        endpoint="update_experiment_permission",
        view_func=update_experiment_permission,
        methods=["PATCH"],
    )
    app.add_url_rule(
        rule=DELETE_EXPERIMENT_PERMISSION,
        endpoint="delete_experiment_permission",
        view_func=delete_experiment_permission,
        methods=["DELETE"],
    )
    app.add_url_rule(
        rule=CREATE_REGISTERED_MODEL_PERMISSION,
        endpoint="create_registered_model_permission",
        view_func=create_registered_model_permission,
        methods=["POST"],
    )
    app.add_url_rule(
        rule=GET_REGISTERED_MODEL_PERMISSION,
        endpoint="get_registered_model_permission",
        view_func=get_registered_model_permission,
        methods=["GET"],
    )
    app.add_url_rule(
        rule=UPDATE_REGISTERED_MODEL_PERMISSION,
        endpoint="update_registered_model_permission",
        view_func=update_registered_model_permission,
        methods=["PATCH"],
    )
    app.add_url_rule(
        rule=DELETE_REGISTERED_MODEL_PERMISSION,
        endpoint="delete_registered_model_permission",
        view_func=delete_registered_model_permission,
        methods=["DELETE"],
    )

    app.before_request(_before_request)
    app.after_request(_after_request)

    return app


def create_asgi_app():
    """
    Create an ASGI-compatible version of the Flask app for uvicorn.
    This function wraps the Flask WSGI app with an ASGI adapter.
    """
    flask_app = create_app()
    
    if ASGI_AVAILABLE:
        # Use WSGI-to-ASGI adapter if available
        return WsgiToAsgi(flask_app)
    else:
        # Fallback: return Flask app directly (may cause compatibility issues)
        _logger.warning(
            "asgiref not available. Install it with: pip install asgiref. "
            "Falling back to direct Flask app (may cause WSGI/ASGI compatibility issues)."
        )
        return flask_app


# The create_app function is exported via pyproject.toml entry point
# No need to create module-level app instance here
